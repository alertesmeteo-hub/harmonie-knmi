<?php
/**
 * AM Stats — amorçage commun : configuration, chargement des classes, utilitaires.
 *
 * Ce fichier n'est jamais appelé directement par le navigateur.
 */

declare(strict_types=1);

if (!defined('AMSTATS')) {
    http_response_code(404);
    exit;
}

const AMSTATS_VERSION = '1.0.0';
define('AMSTATS_ROOT', dirname(__DIR__));

// Jamais d'erreur PHP affichée au visiteur : elles partent dans le journal du serveur.
ini_set('display_errors', '0');
ini_set('log_errors', '1');

spl_autoload_register(static function (string $class): void {
    $prefix = 'AmStats\\';
    if (strncmp($class, $prefix, strlen($prefix)) !== 0) {
        return;
    }
    $name = substr($class, strlen($prefix));
    if (!preg_match('/^[A-Z][A-Za-z]+$/', $name)) {
        return;
    }
    $file = __DIR__ . '/' . $name . '.php';
    if (is_file($file)) {
        require $file;
    }
});

/**
 * Valeurs par défaut, complétées par config.php.
 */
function amstats_default_config(): array
{
    return [
        'allowed_hosts'      => [],
        'site_url'           => '',
        'timezone'           => 'Europe/Paris',
        'db'                 => [
            'driver'       => 'sqlite',
            'sqlite_path'  => AMSTATS_ROOT . '/data/stats.sqlite',
            'table_prefix' => 'am_',
            'mysql'        => [
                'host'     => 'localhost',
                'port'     => 3306,
                'socket'   => '',
                'dbname'   => '',
                'user'     => '',
                'password' => '',
            ],
        ],
        'secret_salt'         => '',
        'admin_password_hash' => '',
        'require_https'       => true,
        'session_idle'        => 7200,
        'session_absolute'    => 43200,
        'trusted_ip_header'   => null,
        'excluded_ips'        => [],
        'excluded_paths'      => [],
        'visit_timeout'       => 1800,
        'retention_days'      => 750,
        'rate_limit'          => 30,
    ];
}

/**
 * Charge (une seule fois) la configuration fusionnée.
 */
function amstats_config(): array
{
    static $config = null;
    if ($config !== null) {
        return $config;
    }

    $file = AMSTATS_ROOT . '/config.php';
    if (!is_file($file)) {
        throw new RuntimeException('Fichier config.php absent : lancez « php setup.php » ou copiez config.sample.php en config.php.');
    }

    $user = require $file;
    if (!is_array($user)) {
        throw new RuntimeException('config.php doit retourner un tableau PHP.');
    }

    $defaults = amstats_default_config();
    $merged = array_merge($defaults, $user);
    $merged['db'] = array_merge($defaults['db'], is_array($user['db'] ?? null) ? $user['db'] : []);
    $merged['db']['mysql'] = array_merge(
        $defaults['db']['mysql'],
        is_array($user['db']['mysql'] ?? null) ? $user['db']['mysql'] : []
    );

    $merged['allowed_hosts'] = array_values(array_unique(array_filter(array_map(
        static fn ($h): string => strtolower(trim((string) $h)),
        (array) $merged['allowed_hosts']
    ))));
    $merged['excluded_paths'] = array_values(array_filter(array_map('strval', (array) $merged['excluded_paths'])));
    $merged['excluded_ips'] = array_values(array_filter(array_map('strval', (array) $merged['excluded_ips'])));

    if (!in_array($merged['timezone'], timezone_identifiers_list(), true)) {
        $merged['timezone'] = 'Europe/Paris';
    }
    date_default_timezone_set($merged['timezone']);

    $config = $merged;
    return $config;
}

/**
 * Le sel secret est-il assez robuste pour produire des empreintes non réversibles ?
 */
function amstats_secret_ok(array $config): bool
{
    return strlen((string) $config['secret_salt']) >= 32;
}

function amstats_is_https(): bool
{
    if (!empty($_SERVER['HTTPS']) && strtolower((string) $_SERVER['HTTPS']) !== 'off') {
        return true;
    }
    if ((int) ($_SERVER['SERVER_PORT'] ?? 0) === 443) {
        return true;
    }
    return strtolower((string) ($_SERVER['HTTP_X_FORWARDED_PROTO'] ?? '')) === 'https';
}

/**
 * Adresse IP du client. Elle sert uniquement à calculer l'empreinte du jour :
 * elle n'est JAMAIS écrite en base ni dans un fichier.
 */
function amstats_client_ip(array $server, array $config): string
{
    $ip = (string) ($server['REMOTE_ADDR'] ?? '');
    $header = $config['trusted_ip_header'] ?? null;
    if (is_string($header) && $header !== '' && !empty($server[$header])) {
        $candidate = trim(explode(',', (string) $server[$header])[0]);
        if (filter_var($candidate, FILTER_VALIDATE_IP)) {
            $ip = $candidate;
        }
    }
    return filter_var($ip, FILTER_VALIDATE_IP) ? $ip : '';
}

/**
 * Teste une IP contre une liste d'IP exactes ou de plages CIDR (IPv4 et IPv6).
 */
function amstats_ip_matches(string $ip, array $list): bool
{
    $bin = @inet_pton($ip);
    if ($bin === false) {
        return false;
    }
    foreach ($list as $entry) {
        $entry = trim((string) $entry);
        if ($entry === '') {
            continue;
        }
        if (strpos($entry, '/') === false) {
            if (@inet_pton($entry) === $bin) {
                return true;
            }
            continue;
        }
        [$net, $bits] = explode('/', $entry, 2);
        $netBin = @inet_pton($net);
        $bits = (int) $bits;
        if ($netBin === false || strlen($netBin) !== strlen($bin) || $bits < 0 || $bits > strlen($bin) * 8) {
            continue;
        }
        $bytes = intdiv($bits, 8);
        $rest = $bits % 8;
        if (strncmp($bin, $netBin, $bytes) !== 0) {
            continue;
        }
        if ($rest === 0) {
            return true;
        }
        $mask = (0xFF << (8 - $rest)) & 0xFF;
        if ((ord($bin[$bytes]) & $mask) === (ord($netBin[$bytes]) & $mask)) {
            return true;
        }
    }
    return false;
}

/**
 * Coupe une chaîne UTF-8 sans casser de caractère multi-octets.
 */
function amstats_truncate(string $value, int $length): string
{
    if (function_exists('mb_substr')) {
        return mb_substr($value, 0, $length, 'UTF-8');
    }
    if (strlen($value) <= $length) {
        return $value;
    }
    return preg_match('/^.{0,' . $length . '}/us', $value, $m) ? $m[0] : substr($value, 0, $length);
}

function amstats_is_utf8(string $value): bool
{
    if (function_exists('mb_check_encoding')) {
        return mb_check_encoding($value, 'UTF-8');
    }
    return preg_match('//u', $value) === 1;
}

function amstats_h(?string $value): string
{
    return htmlspecialchars((string) $value, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}

/**
 * En-têtes de sécurité du tableau de bord (CSP stricte : aucun script ni style inline).
 */
function amstats_dashboard_headers(): void
{
    header("Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'");
    header('X-Frame-Options: DENY');
    header('X-Content-Type-Options: nosniff');
    header('Referrer-Policy: no-referrer');
    header('Cross-Origin-Opener-Policy: same-origin');
    header('Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), usb=()');
    header('X-Robots-Tag: noindex, nofollow, noarchive');
    header('Cache-Control: no-store, max-age=0');
}

function amstats_json(array $data, int $status = 200): void
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store, max-age=0');
    header('X-Content-Type-Options: nosniff');
    header('X-Robots-Tag: noindex, nofollow');
    echo json_encode($data, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_PRESERVE_ZERO_FRACTION);
}
