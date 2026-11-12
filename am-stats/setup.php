<?php
/**
 * AM Stats — assistant d'installation en ligne de commande.
 *
 *   php setup.php            crée config.php (sel secret + mot de passe) à partir de config.sample.php
 *   php setup.php password   change le mot de passe dans un config.php existant
 *   php setup.php check      vérifie l'environnement (extensions PHP, droits d'écriture, base)
 *
 * Inaccessible depuis le web.
 */

declare(strict_types=1);

if (PHP_SAPI !== 'cli') {
    http_response_code(404);
    exit;
}

$dir = __DIR__;
$configFile = $dir . '/config.php';
$sampleFile = $dir . '/config.sample.php';
$command = $argv[1] ?? 'init';

function out(string $msg): void
{
    fwrite(STDOUT, $msg . PHP_EOL);
}

function fail(string $msg): void
{
    fwrite(STDERR, '✖ ' . $msg . PHP_EOL);
    exit(1);
}

function ask(string $question, bool $hidden = false): string
{
    fwrite(STDOUT, $question);
    $tty = $hidden && DIRECTORY_SEPARATOR === '/' && function_exists('shell_exec') && posix_isatty_safe();
    if ($tty) {
        shell_exec('stty -echo');
    }
    $line = fgets(STDIN);
    if ($tty) {
        shell_exec('stty echo');
        fwrite(STDOUT, PHP_EOL);
    }
    return $line === false ? '' : rtrim($line, "\r\n");
}

function posix_isatty_safe(): bool
{
    return function_exists('posix_isatty') ? @posix_isatty(STDIN) : true;
}

function ask_password(): string
{
    for ($i = 0; $i < 3; $i++) {
        $p1 = ask('Mot de passe du tableau de bord (12 caractères min.) : ', true);
        if (strlen($p1) < 12) {
            out('  → trop court.');
            continue;
        }
        $p2 = ask('Confirmation : ', true);
        if (!hash_equals($p1, $p2)) {
            out('  → les deux saisies diffèrent.');
            continue;
        }
        return $p1;
    }
    fail('Abandon.');
    return '';
}

function password_hash_best(string $password): string
{
    $algo = defined('PASSWORD_ARGON2ID') ? PASSWORD_ARGON2ID : PASSWORD_DEFAULT;
    return password_hash($password, $algo);
}

function set_config_value(string $content, string $key, string $value): string
{
    $pattern = "~('" . preg_quote($key, '~') . "'\s*=>\s*)'[^'\\\\]*(?:\\\\.[^'\\\\]*)*'~";
    $count = 0;
    $result = preg_replace_callback($pattern, static fn (array $m): string => $m[1] . var_export($value, true), $content, 1, $count);
    if ($count !== 1 || $result === null) {
        fail("Clé « $key » introuvable dans config.php.");
    }
    return $result;
}

switch ($command) {
    case 'init':
        if (is_file($configFile)) {
            fail('config.php existe déjà. Utilisez « php setup.php password » pour changer le mot de passe.');
        }
        if (!is_file($sampleFile)) {
            fail('config.sample.php introuvable.');
        }
        out('== AM Stats : création de config.php ==');
        $content = (string) file_get_contents($sampleFile);

        $hostsInput = ask('Domaines du site, séparés par des virgules [www.alertes-meteo.com, alertes-meteo.com] : ');
        if (trim($hostsInput) !== '') {
            $hosts = array_values(array_filter(array_map(
                static fn (string $h): string => strtolower(trim(preg_replace('~^https?://|/.*$~i', '', trim($h)) ?? '')),
                explode(',', $hostsInput)
            )));
            if (!$hosts) {
                fail('Aucun domaine valide.');
            }
            $export = "['" . implode("', '", array_map('addslashes', $hosts)) . "']";
            $content = (string) preg_replace("~('allowed_hosts'\s*=>\s*)\[[^\]]*\]~", '${1}' . $export, $content, 1);
            $content = set_config_value($content, 'site_url', 'https://' . $hosts[0]);
        }

        $content = set_config_value($content, 'secret_salt', bin2hex(random_bytes(32)));
        $content = set_config_value($content, 'admin_password_hash', password_hash_best(ask_password()));

        if (file_put_contents($configFile, $content, LOCK_EX) === false) {
            fail('Écriture de config.php impossible.');
        }
        @chmod($configFile, 0640);
        out('✔ config.php créé (sel secret de 256 bits + mot de passe haché).');
        out('  Vérifiez maintenant l\'installation : php setup.php check');
        break;

    case 'password':
        if (!is_file($configFile)) {
            fail('config.php absent : lancez d\'abord « php setup.php ».');
        }
        $content = (string) file_get_contents($configFile);
        $content = set_config_value($content, 'admin_password_hash', password_hash_best(ask_password()));
        if (file_put_contents($configFile, $content, LOCK_EX) === false) {
            fail('Écriture de config.php impossible.');
        }
        out('✔ Mot de passe mis à jour.');
        break;

    case 'check':
        define('AMSTATS', true);
        require $dir . '/lib/bootstrap.php';
        $ok = true;
        $line = static function (bool $pass, string $label) use (&$ok): void {
            $ok = $ok && $pass;
            out(($pass ? '✔ ' : '✖ ') . $label);
        };
        $line(PHP_VERSION_ID >= 70400, 'PHP ' . PHP_VERSION . ' (7.4 minimum, 8.1+ recommandé)');
        $line(extension_loaded('pdo'), 'Extension PDO');
        $line(function_exists('hash_hmac') && in_array('sha256', hash_hmac_algos(), true), 'HMAC-SHA256');
        out((extension_loaded('mbstring') ? '✔ ' : '• ') . 'mbstring (facultatif)');
        out((function_exists('apcu_fetch') ? '✔ ' : '• ') . 'APCu (facultatif, met le sel du jour en cache)');
        try {
            $config = amstats_config();
            $line(true, 'config.php chargé');
            $line(amstats_secret_ok($config), 'secret_salt défini (32 caractères minimum)');
            $line((string) $config['admin_password_hash'] !== '', 'admin_password_hash défini');
            $line(!empty($config['allowed_hosts']), 'allowed_hosts : ' . implode(', ', $config['allowed_hosts']));
            $line(extension_loaded($config['db']['driver'] === 'mysql' ? 'pdo_mysql' : 'pdo_sqlite'), 'Pilote PDO ' . $config['db']['driver']);
            $db = new AmStats\Database($config['db']);
            $db->createSchema();
            $line(true, 'Base de données accessible, tables et index créés');
            if ($config['db']['driver'] === 'sqlite') {
                $path = $config['db']['sqlite_path'];
                $line(is_writable(dirname($path)), 'Dossier SQLite inscriptible : ' . dirname($path));
                $mode = (string) $db->pdo()->query('PRAGMA journal_mode')->fetchColumn();
                $line(strtolower($mode) === 'wal', 'SQLite en mode WAL (écritures concurrentes)');
            }
        } catch (Throwable $e) {
            $line(false, $e->getMessage());
        }
        out($ok ? 'Tout est prêt.' : 'Corrigez les points en ✖ ci-dessus.');
        exit($ok ? 0 : 1);

    default:
        out('Usage : php setup.php [init|password|check]');
        exit(1);
}
