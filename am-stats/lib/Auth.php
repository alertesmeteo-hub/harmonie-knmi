<?php
/**
 * AM Stats — authentification du tableau de bord.
 *
 * - Mot de passe vérifié avec password_verify() (bcrypt / Argon2id).
 * - Session durcie : cookie HttpOnly, Secure, SameSite=Strict, identifiant régénéré.
 * - Jeton CSRF sur tous les formulaires.
 * - Anti-force brute : 5 échecs en 15 min ⇒ blocage de 15 min (clé = HMAC de l'IP).
 */

declare(strict_types=1);

namespace AmStats;

if (!\defined('AMSTATS')) {
    \http_response_code(404);
    exit;
}

final class Auth
{
    private const MAX_ATTEMPTS = 5;
    private const WINDOW = 900;
    private const LOCK = 900;

    private Database $db;
    private array $config;

    public function __construct(Database $db, array $config)
    {
        $this->db = $db;
        $this->config = $config;
    }

    public function startSession(): void
    {
        if (session_status() === PHP_SESSION_ACTIVE) {
            return;
        }

        ini_set('session.use_strict_mode', '1');
        ini_set('session.use_only_cookies', '1');
        ini_set('session.use_trans_sid', '0');
        ini_set('session.cookie_httponly', '1');

        session_name('amstats_sid');
        session_set_cookie_params([
            'lifetime' => 0,
            'path'     => self::cookiePath(),
            'secure'   => amstats_is_https(),
            'httponly' => true,
            'samesite' => 'Strict',
        ]);
        session_start();

        if (!empty($_SESSION['auth'])) {
            $now = time();
            $expired = ($now - (int) ($_SESSION['last'] ?? 0)) > (int) $this->config['session_idle']
                || ($now - (int) ($_SESSION['created'] ?? 0)) > (int) $this->config['session_absolute']
                || !hash_equals((string) ($_SESSION['ua'] ?? ''), self::uaFingerprint());
            if ($expired) {
                $this->logout();
                session_start();
            } else {
                $_SESSION['last'] = $now;
            }
        }

        if (empty($_SESSION['csrf'])) {
            $_SESSION['csrf'] = bin2hex(random_bytes(32));
        }
    }

    public function isLoggedIn(): bool
    {
        return !empty($_SESSION['auth']);
    }

    public function csrfToken(): string
    {
        return (string) ($_SESSION['csrf'] ?? '');
    }

    public function checkCsrf($token): bool
    {
        $expected = $this->csrfToken();
        return $expected !== '' && is_string($token) && hash_equals($expected, $token);
    }

    public function isConfigured(): bool
    {
        $hash = (string) $this->config['admin_password_hash'];
        return $hash !== '' && password_get_info($hash)['algoName'] !== 'unknown';
    }

    /**
     * @return array{ok:bool, error:?string}
     */
    public function attemptLogin(string $password, string $ip): array
    {
        if (!$this->isConfigured()) {
            return ['ok' => false, 'error' => 'Aucun mot de passe n\'est configuré (admin_password_hash vide dans config.php).'];
        }

        $now = time();
        $key = hash_hmac('sha256', 'login|' . $ip, (string) $this->config['secret_salt']);

        return $this->db->withSchema(function () use ($password, $now, $key): array {
            $row = $this->db->run('SELECT attempts, first_at, locked_until FROM {logins} WHERE id = :id', [':id' => $key])->fetch();

            if ($row && (int) $row['locked_until'] > $now) {
                $minutes = (int) ceil(((int) $row['locked_until'] - $now) / 60);
                return ['ok' => false, 'error' => sprintf('Trop de tentatives. Réessayez dans %d minute%s.', $minutes, $minutes > 1 ? 's' : '')];
            }

            if (password_verify($password, (string) $this->config['admin_password_hash'])) {
                $this->db->run('DELETE FROM {logins} WHERE id = :id', [':id' => $key]);
                session_regenerate_id(true);
                $_SESSION = [
                    'auth'    => true,
                    'created' => $now,
                    'last'    => $now,
                    'ua'      => self::uaFingerprint(),
                    'csrf'    => bin2hex(random_bytes(32)),
                ];
                return ['ok' => true, 'error' => null];
            }

            // Échec : compteur sur une fenêtre glissante de 15 minutes.
            if (!$row || ($now - (int) $row['first_at']) > self::WINDOW) {
                $this->db->run('DELETE FROM {logins} WHERE id = :id', [':id' => $key]);
                $this->db->run(
                    'INSERT INTO {logins} (id, attempts, first_at, locked_until) VALUES (:id, 1, :now, 0)',
                    [':id' => $key, ':now' => $now]
                );
                $attempts = 1;
            } else {
                $attempts = (int) $row['attempts'] + 1;
                $lockedUntil = $attempts >= self::MAX_ATTEMPTS ? $now + self::LOCK : 0;
                $this->db->run(
                    'UPDATE {logins} SET attempts = :a, locked_until = :l WHERE id = :id',
                    [':a' => $attempts, ':l' => $lockedUntil, ':id' => $key]
                );
            }

            usleep(random_int(400000, 800000)); // ralentit les attaques automatisées

            $left = self::MAX_ATTEMPTS - $attempts;
            if ($left <= 0) {
                return ['ok' => false, 'error' => 'Trop de tentatives. Accès bloqué pendant 15 minutes.'];
            }
            return ['ok' => false, 'error' => sprintf('Mot de passe incorrect (%d essai%s restant%s).', $left, $left > 1 ? 's' : '', $left > 1 ? 's' : '')];
        });
    }

    public function logout(): void
    {
        $_SESSION = [];
        if (session_status() === PHP_SESSION_ACTIVE) {
            $params = session_get_cookie_params();
            setcookie(session_name(), '', [
                'expires'  => time() - 3600,
                'path'     => $params['path'],
                'secure'   => $params['secure'],
                'httponly' => true,
                'samesite' => 'Strict',
            ]);
            session_destroy();
        }
    }

    public static function cookiePath(): string
    {
        $dir = str_replace('\\', '/', dirname((string) ($_SERVER['SCRIPT_NAME'] ?? '/')));
        return rtrim($dir, '/') . '/';
    }

    private static function uaFingerprint(): string
    {
        return hash('sha256', (string) ($_SERVER['HTTP_USER_AGENT'] ?? ''));
    }
}
