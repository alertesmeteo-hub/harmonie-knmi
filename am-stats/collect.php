<?php
/**
 * AM Stats — point d'entrée de la sonde (reçoit les navigator.sendBeacon).
 *
 * Réponses :
 *   204  page vue enregistrée, ou ignorée volontairement (robot, IP exclue…)
 *   400  données invalides          403  envoi depuis un domaine non autorisé
 *   405  méthode autre que POST     413  requête trop volumineuse
 *   503  configuration incomplète   500  erreur serveur (voir le journal PHP)
 * L'en-tête « X-AM-Stats » indique la raison (utile pour l'intégration).
 */

declare(strict_types=1);

define('AMSTATS', true);
require __DIR__ . '/lib/bootstrap.php';

use AmStats\Collector;
use AmStats\Database;

header('Cache-Control: no-store, max-age=0');
header('X-Content-Type-Options: nosniff');
header('X-Robots-Tag: noindex, nofollow');

$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';
if ($method === 'OPTIONS') {
    http_response_code(204);
    exit;
}
if ($method !== 'POST') {
    header('Allow: POST');
    http_response_code(405);
    exit;
}

if ((int) ($_SERVER['CONTENT_LENGTH'] ?? 0) > 4096) {
    http_response_code(413);
    exit;
}

$collector = null;
$status = 500;
$reason = 'error';

try {
    $config = amstats_config();
    $raw = (string) file_get_contents('php://input', false, null, 0, 4097);
    if (strlen($raw) > 4096) {
        $status = 413;
        $reason = 'too-large';
    } else {
        $collector = new Collector(new Database($config['db']), $config);
        [$status, $reason] = $collector->process($_SERVER, $raw);
    }
} catch (Throwable $e) {
    error_log('[am-stats] collect : ' . $e->getMessage());
    $status = 500;
    $reason = 'error';
}

http_response_code($status);
header('X-AM-Stats: ' . $reason);
header('Content-Length: 0');

// La réponse part immédiatement ; la maintenance éventuelle se fait ensuite.
if (function_exists('fastcgi_finish_request')) {
    fastcgi_finish_request();
} elseif (function_exists('litespeed_finish_request')) {
    litespeed_finish_request();
} else {
    flush();
}

// Purge des données expirées : environ une requête sur 500.
if ($collector !== null && $status === 204 && random_int(1, 500) === 1) {
    try {
        $collector->maintenance();
    } catch (Throwable $e) {
        error_log('[am-stats] maintenance : ' . $e->getMessage());
    }
}
