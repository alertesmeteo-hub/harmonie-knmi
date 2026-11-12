<?php
/**
 * AM Stats — réception d'une page vue : filtrage, empreinte anonyme, enregistrement.
 */

declare(strict_types=1);

namespace AmStats;

if (!\defined('AMSTATS')) {
    \http_response_code(404);
    exit;
}

use DateTimeImmutable;
use DateTimeZone;

final class Collector
{
    /** Types d'appareil et leur code en base. */
    public const DEVICES = ['mobile' => 1, 'tablet' => 2, 'desktop' => 3];

    /**
     * Robots, crawlers, outils de monitoring, aperçus de liens, navigateurs
     * automatisés et bibliothèques HTTP. « (?<!cu)bot » évite d'exclure les
     * smartphones de la marque Cubot.
     */
    private const BOT_UA = '~(?<!cu)bot|crawl|spider|slurp|scrap|archiver|fetch|curl|wget|python|java/|httpclient|okhttp|go-http|axios|undici|libwww|perl|ruby|guzzle|php/|headless|phantom|selenium|puppeteer|playwright|webdriver|lighthouse|pagespeed|gtmetrix|pingdom|uptime|monitor|statuscake|site24x7|facebookexternalhit|facebookcatalog|meta-externalagent|embedly|whatsapp|skypeuripreview|preview|validator|mediapartners|google-inspectiontool|chatgpt-user|oai-searchbot|claude-|anthropic|perplexity|ccbot|bytespider|amazonbot|petalbot|semrush|ahrefs|mj12|zgrab|nmap|masscan|nikto|sqlmap|censys|expanse~i';

    private Database $db;
    private array $config;

    public function __construct(Database $db, array $config)
    {
        $this->db = $db;
        $this->config = $config;
    }

    public static function isBot(string $userAgent): bool
    {
        $len = strlen($userAgent);
        return $len < 12 || $len > 512 || preg_match(self::BOT_UA, $userAgent) === 1;
    }

    /**
     * Traite une requête de la sonde.
     *
     * @param array  $server  équivalent de $_SERVER
     * @param string $raw     corps brut de la requête (JSON)
     * @return array{0:int,1:string} [code HTTP, raison]
     */
    public function process(array $server, string $raw, ?int $now = null): array
    {
        $now = $now ?? time();

        if (!amstats_secret_ok($this->config)) {
            return [503, 'secret-salt-missing'];
        }

        $ua = trim((string) ($server['HTTP_USER_AGENT'] ?? ''));
        if (self::isBot($ua)) {
            return [204, 'bot'];
        }

        if (!$this->originAllowed($server)) {
            return [403, 'origin'];
        }

        $data = json_decode($raw, true, 4);
        if (!is_array($data)) {
            return [400, 'payload'];
        }

        $parts = parse_url(is_string($data['u'] ?? null) ? $data['u'] : '');
        if (!is_array($parts) || empty($parts['host'])
            || !in_array(strtolower((string) $parts['host']), $this->config['allowed_hosts'], true)) {
            return [400, 'url'];
        }

        $path = self::normalizePath((string) ($parts['path'] ?? '/'));
        foreach ($this->config['excluded_paths'] as $prefix) {
            if ($prefix !== '' && strncmp($path, $prefix, strlen($prefix)) === 0) {
                return [204, 'excluded-path'];
            }
        }

        $ip = amstats_client_ip($server, $this->config);
        if ($ip === '') {
            return [400, 'ip'];
        }
        if (amstats_ip_matches($ip, $this->config['excluded_ips'])) {
            return [204, 'excluded-ip'];
        }

        $deviceName = is_string($data['d'] ?? null) && isset(self::DEVICES[$data['d']]) ? $data['d'] : self::deviceFromUserAgent($ua);
        $device = self::DEVICES[$deviceName];
        $referrer = is_string($data['r'] ?? null) ? substr($data['r'], 0, 2000) : '';
        $utm = is_string($data['s'] ?? null) ? substr($data['s'], 0, 200) : '';

        $local = (new DateTimeImmutable('@' . $now))->setTimezone(new DateTimeZone($this->config['timezone']));
        $day = (int) $local->format('Ymd');
        $hour = (int) $local->format('G');

        return $this->db->withSchema(function () use ($day, $hour, $ip, $ua, $now, $path, $referrer, $utm, $device): array {
            $visitor = $this->visitorHash($day, $ip, $ua);

            $activity = $this->db->run(
                'SELECT MAX(ts) AS last_ts, SUM(CASE WHEN ts >= :minute THEN 1 ELSE 0 END) AS recent
                   FROM {pageviews} WHERE visitor = :visitor AND ts >= :since',
                [
                    ':minute'  => $now - 60,
                    ':visitor' => $visitor,
                    ':since'   => $now - (int) $this->config['visit_timeout'],
                ]
            )->fetch();

            if ((int) ($activity['recent'] ?? 0) >= (int) $this->config['rate_limit']) {
                return [204, 'rate-limited'];
            }

            // Nouvelle visite si aucune page vue depuis « visit_timeout » secondes (30 min par défaut).
            $isEntry = empty($activity['last_ts']);
            $source = $isEntry ? Referrer::source($referrer, $utm, $this->config['allowed_hosts']) : null;

            $this->db->run(
                'INSERT INTO {pageviews} (ts, day, hour, visitor, path_id, source, device)
                 VALUES (:ts, :day, :hour, :visitor, :path_id, :source, :device)',
                [
                    ':ts'      => $now,
                    ':day'     => $day,
                    ':hour'    => $hour,
                    ':visitor' => $visitor,
                    ':path_id' => $this->pathId($path),
                    ':source'  => $source,
                    ':device'  => $device,
                ]
            );

            return [204, $isEntry ? 'ok-entry' : 'ok'];
        });
    }

    /**
     * Empreinte visiteur du jour, anonyme et non réversible :
     *   HMAC-SHA256( date | IP | User-Agent , sel secret permanent + sel aléatoire du jour )
     * dont on conserve les 64 premiers bits sous forme d'entier (8 octets en base).
     * Collision : probabilité < 1 sur 10 millions pour 1 million de visiteurs par jour.
     *
     * - L'IP n'est jamais stockée ; en IPv6 on n'utilise que le préfixe /64
     *   (les extensions de confidentialité changent la fin de l'adresse).
     * - Le sel du jour est détruit le lendemain : même avec un accès complet
     *   au serveur, il devient impossible de recalculer une empreinte passée.
     * - L'empreinte change chaque jour : aucun suivi d'un jour sur l'autre.
     */
    public function visitorHash(int $day, string $ip, string $userAgent): int
    {
        $key = (string) $this->config['secret_salt'] . $this->dailySalt($day);
        $message = $day . "\n" . self::ipForHash($ip) . "\n" . $userAgent;
        $digest = hash_hmac('sha256', $message, $key, true);
        return (int) unpack('J', substr($digest, 0, 8))[1];
    }

    /**
     * Identifiant du chemin dans le dictionnaire {paths} (créé au besoin).
     */
    private function pathId(string $path): int
    {
        $cacheKey = 'amstats_path_' . $this->db->table('paths') . '_' . md5($path);
        $apcu = function_exists('apcu_fetch') && filter_var(ini_get('apc.enabled'), FILTER_VALIDATE_BOOLEAN);
        if ($apcu) {
            $cached = apcu_fetch($cacheKey, $found);
            if ($found && is_int($cached)) {
                return $cached;
            }
        }

        $id = $this->db->run('SELECT id FROM {paths} WHERE path = :path', [':path' => $path])->fetchColumn();
        if ($id === false) {
            $this->db->run($this->db->insertIgnore() . ' INTO {paths} (path) VALUES (:path)', [':path' => $path]);
            $id = $this->db->run('SELECT id FROM {paths} WHERE path = :path', [':path' => $path])->fetchColumn();
        }
        $id = (int) $id;

        if ($apcu) {
            apcu_store($cacheKey, $id, 86400);
        }
        return $id;
    }

    private function dailySalt(int $day): string
    {
        $cacheKey = 'amstats_salt_' . $this->db->table('salts') . '_' . $day;
        $apcu = function_exists('apcu_fetch') && filter_var(ini_get('apc.enabled'), FILTER_VALIDATE_BOOLEAN);
        if ($apcu) {
            $cached = apcu_fetch($cacheKey, $found);
            if ($found && is_string($cached)) {
                return $cached;
            }
        }

        $salt = $this->db->run('SELECT salt FROM {salts} WHERE day = :day', [':day' => $day])->fetchColumn();
        if (!is_string($salt) || $salt === '') {
            $this->db->run(
                $this->db->insertIgnore() . ' INTO {salts} (day, salt) VALUES (:day, :salt)',
                [':day' => $day, ':salt' => bin2hex(random_bytes(32))]
            );
            // Nouveau jour : on détruit immédiatement les sels des jours précédents.
            $this->db->run('DELETE FROM {salts} WHERE day < :day', [':day' => $day]);
            $salt = (string) $this->db->run('SELECT salt FROM {salts} WHERE day = :day', [':day' => $day])->fetchColumn();
        }

        if ($apcu) {
            apcu_store($cacheKey, $salt, 90000);
        }
        return $salt;
    }

    private static function ipForHash(string $ip): string
    {
        if (strpos($ip, ':') !== false) {
            $bin = @inet_pton($ip);
            if ($bin !== false && strlen($bin) === 16) {
                return bin2hex(substr($bin, 0, 8)) . '::/64';
            }
        }
        return $ip;
    }

    /**
     * Rejette les envois provenant d'un autre site (Origin / Referer).
     */
    private function originAllowed(array $server): bool
    {
        $allowed = $this->config['allowed_hosts'];
        if (!$allowed) {
            return false;
        }

        $origin = trim((string) ($server['HTTP_ORIGIN'] ?? ''));
        if ($origin !== '' && $origin !== 'null') {
            $host = parse_url($origin, PHP_URL_HOST);
            return is_string($host) && in_array(strtolower($host), $allowed, true);
        }

        $referer = trim((string) ($server['HTTP_REFERER'] ?? ''));
        if ($referer !== '') {
            $host = parse_url($referer, PHP_URL_HOST);
            return is_string($host) && in_array(strtolower($host), $allowed, true);
        }

        // Ni Origin ni Referer (politique « no-referrer ») : l'URL de la page est vérifiée ensuite.
        return true;
    }

    public static function normalizePath(string $path): string
    {
        $path = '/' . ltrim($path, '/');
        $decoded = rawurldecode($path);
        if (amstats_is_utf8($decoded)) {
            $path = $decoded;
        }
        $path = (string) preg_replace('~[\x00-\x1F\x7F]~', '', $path);
        $path = (string) preg_replace('~/{2,}~', '/', $path);
        $path = amstats_truncate($path, 500);
        return $path === '' ? '/' : $path;
    }

    /** Repli si la sonde n'a pas transmis le type d'appareil. */
    public static function deviceFromUserAgent(string $ua): string
    {
        if (preg_match('~iPad|Tablet|PlayBook|Silk|Kindle|Android(?!.*Mobile)~i', $ua)) {
            return 'tablet';
        }
        if (preg_match('~Mobi|iPhone|iPod|Android|Windows Phone~i', $ua)) {
            return 'mobile';
        }
        return 'desktop';
    }

    /**
     * Maintenance occasionnelle (appelée après la réponse HTTP) :
     * purge des données au-delà de la durée de conservation, des sels périmés
     * et des verrous de connexion expirés.
     */
    public function maintenance(?int $now = null): void
    {
        $now = $now ?? time();
        $tz = new DateTimeZone($this->config['timezone']);
        $today = (new DateTimeImmutable('@' . $now))->setTimezone($tz);
        $cutoff = (int) $today->modify('-' . max(1, (int) $this->config['retention_days']) . ' days')->format('Ymd');

        $this->db->withSchema(function () use ($cutoff, $today, $now): void {
            $this->db->run('DELETE FROM {pageviews} WHERE day < :cutoff', [':cutoff' => $cutoff]);
            $this->db->run('DELETE FROM {salts} WHERE day < :today', [':today' => (int) $today->format('Ymd')]);
            $this->db->run(
                'DELETE FROM {logins} WHERE locked_until < :now AND first_at < :old',
                [':now' => $now, ':old' => $now - 86400]
            );
            if ($this->db->driver() === 'sqlite') {
                $this->db->pdo()->exec('PRAGMA optimize');
            }
        });
    }
}
