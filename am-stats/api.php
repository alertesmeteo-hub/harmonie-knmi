<?php
/**
 * AM Stats — API JSON du tableau de bord (accès réservé à l'administrateur connecté).
 *
 *   api.php?action=summary&period=today|yesterday|7d|30d
 *   api.php?action=summary&period=custom&from=2026-10-01&to=2026-10-31
 *   api.php?action=realtime
 */

declare(strict_types=1);

define('AMSTATS', true);
require __DIR__ . '/lib/bootstrap.php';

use AmStats\Auth;
use AmStats\Database;
use AmStats\Stats;

try {
    $config = amstats_config();

    if ($config['require_https'] && !amstats_is_https()) {
        amstats_json(['error' => 'HTTPS requis.'], 403);
        exit;
    }
    if (($_SERVER['REQUEST_METHOD'] ?? 'GET') !== 'GET') {
        header('Allow: GET');
        amstats_json(['error' => 'Méthode non autorisée.'], 405);
        exit;
    }

    $db = new Database($config['db']);
    $auth = new Auth($db, $config);
    $auth->startSession();
    $loggedIn = $auth->isLoggedIn();
    session_write_close(); // libère le verrou : les appels parallèles ne s'attendent pas

    if (!$loggedIn) {
        amstats_json(['error' => 'Session expirée : reconnectez-vous.'], 401);
        exit;
    }

    $stats = new Stats($db, $config);
    $action = (string) ($_GET['action'] ?? 'summary');

    if ($action === 'realtime') {
        $data = $db->withSchema(static fn (): array => $stats->realtime());
        amstats_json($data);
        exit;
    }

    if ($action !== 'summary') {
        amstats_json(['error' => 'Action inconnue.'], 400);
        exit;
    }

    try {
        $range = Stats::resolvePeriod(
            (string) ($_GET['period'] ?? '7d'),
            isset($_GET['from']) ? (string) $_GET['from'] : null,
            isset($_GET['to']) ? (string) $_GET['to'] : null,
            $config['timezone']
        );
    } catch (InvalidArgumentException $e) {
        amstats_json(['error' => $e->getMessage()], 400);
        exit;
    }

    $data = $db->withSchema(static fn (): array => $stats->summary($range, 20));
    amstats_json($data);
} catch (Throwable $e) {
    error_log('[am-stats] api : ' . $e->getMessage());
    amstats_json(['error' => 'Erreur serveur : consultez le journal PHP.'], 500);
}
