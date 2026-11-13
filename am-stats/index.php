<?php
/**
 * AM Stats — tableau de bord (connexion + interface de visualisation).
 */

declare(strict_types=1);

define('AMSTATS', true);
require __DIR__ . '/lib/bootstrap.php';

use AmStats\Auth;
use AmStats\Database;

amstats_dashboard_headers();

$config = null;
$auth = null;
$fatal = null;
$error = null;
$generated = null;

try {
    $config = amstats_config();
} catch (Throwable $e) {
    $fatal = $e->getMessage();
}

if ($fatal === null && $config['require_https'] && !amstats_is_https()) {
    $host = (string) ($_SERVER['HTTP_HOST'] ?? '');
    if (preg_match('/^[A-Za-z0-9.\-]+(:\d{1,5})?$/', $host)) {
        header('Location: https://' . $host . Auth::cookiePath(), true, 301);
        exit;
    }
    $fatal = 'Le tableau de bord n\'est accessible qu\'en HTTPS.';
}

if ($fatal === null) {
    try {
        $db = new Database($config['db']);
        $db->ensureSchema();
        $auth = new Auth($db, $config);
        $auth->startSession();

        if (($_SERVER['REQUEST_METHOD'] ?? 'GET') === 'POST') {
            $action = (string) ($_POST['action'] ?? '');

            if (!$auth->checkCsrf($_POST['csrf'] ?? null)) {
                $error = 'Formulaire expiré : rechargez la page puis réessayez.';
            } elseif ($action === 'login') {
                $result = $auth->attemptLogin((string) ($_POST['password'] ?? ''), amstats_client_ip($_SERVER, $config));
                if ($result['ok']) {
                    header('Location: ' . Auth::cookiePath(), true, 303);
                    exit;
                }
                $error = $result['error'];
            } elseif ($action === 'logout') {
                $auth->logout();
                header('Location: ' . Auth::cookiePath(), true, 303);
                exit;
            } elseif ($action === 'generate' && !$auth->isConfigured()) {
                // Assistant d'installation : calcule le hash à copier dans config.php.
                // Rien n'est écrit sur le serveur.
                $pwd = (string) ($_POST['new_password'] ?? '');
                if (strlen($pwd) < 12) {
                    $error = 'Choisissez un mot de passe d\'au moins 12 caractères.';
                } elseif (!hash_equals($pwd, (string) ($_POST['new_password_confirm'] ?? ''))) {
                    $error = 'Les deux mots de passe saisis sont différents.';
                } else {
                    $algo = defined('PASSWORD_ARGON2ID') ? PASSWORD_ARGON2ID : PASSWORD_DEFAULT;
                    $generated = [
                        'hash' => password_hash($pwd, $algo),
                        'salt' => amstats_secret_ok($config) ? null : bin2hex(random_bytes(32)),
                    ];
                }
            }
        }
    } catch (Throwable $e) {
        error_log('[am-stats] tableau de bord : ' . $e->getMessage());
        $fatal = 'Base de données inaccessible. Vérifiez les droits d\'écriture du dossier data/ (SQLite) ou les identifiants MySQL dans config.php. Détail dans le journal d\'erreurs PHP.';
    }
}

$loggedIn = $auth !== null && $fatal === null && $auth->isLoggedIn();
$csrf = $auth !== null ? $auth->csrfToken() : '';
$v = AMSTATS_VERSION;
$favicon = 'data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 32 32%27%3E%3Crect width=%2732%27 height=%2732%27 rx=%277%27 fill=%27%232a78d6%27/%3E%3Cpath d=%27M7 22l6-7 5 4 7-9%27 stroke=%27white%27 stroke-width=%273%27 fill=%27none%27 stroke-linecap=%27round%27 stroke-linejoin=%27round%27/%3E%3C/svg%3E';
$logo = '<svg class="logo" viewBox="0 0 32 32" aria-hidden="true" focusable="false"><rect width="32" height="32" rx="7"/><path d="M7 22l6-7 5 4 7-9"/></svg>';
?>
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="color-scheme" content="light dark">
<title><?= $loggedIn ? 'AM Stats — Tableau de bord' : 'AM Stats — Connexion' ?></title>
<link rel="icon" href="<?= amstats_h($favicon) ?>">
<link rel="stylesheet" href="assets/dashboard.css?v=<?= amstats_h($v) ?>">
</head>
<?php if (!$loggedIn): ?>
<body class="auth">
<main class="auth-box">
    <div class="brand"><?= $logo ?><span class="brand-name">AM Stats</span></div>

    <?php if ($fatal !== null): ?>
        <h1>Configuration requise</h1>
        <p class="alert alert-error" role="alert"><?= amstats_h($fatal) ?></p>

    <?php elseif (!$auth->isConfigured()): ?>
        <h1>Installation</h1>
        <p class="alert alert-info">Aucun mot de passe n'est encore configuré. Choisissez-en un : l'outil calcule la ligne à coller dans <code>config.php</code>. Rien n'est enregistré sur le serveur.</p>
        <?php if ($error !== null): ?><p class="alert alert-error" role="alert"><?= amstats_h($error) ?></p><?php endif; ?>

        <?php if ($generated !== null): ?>
            <p>Copiez cette ligne dans <code>config.php</code> (clé <code>admin_password_hash</code>) :</p>
            <pre class="code"><code><?= amstats_h("'admin_password_hash' => " . var_export($generated['hash'], true) . ',') ?></code></pre>
            <?php if ($generated['salt'] !== null): ?>
                <p>Votre sel secret est vide ou trop court : ajoutez aussi cette ligne (clé <code>secret_salt</code>) :</p>
                <pre class="code"><code><?= amstats_h("'secret_salt' => " . var_export($generated['salt'], true) . ',') ?></code></pre>
            <?php endif; ?>
            <p class="muted">Enregistrez config.php puis rechargez cette page pour vous connecter.</p>
        <?php else: ?>
            <form method="post" class="auth-form" autocomplete="off">
                <input type="hidden" name="csrf" value="<?= amstats_h($csrf) ?>">
                <input type="hidden" name="action" value="generate">
                <label for="new_password">Nouveau mot de passe <span class="muted">(12 caractères min.)</span></label>
                <input type="password" id="new_password" name="new_password" minlength="12" required autocomplete="new-password">
                <label for="new_password_confirm">Confirmation</label>
                <input type="password" id="new_password_confirm" name="new_password_confirm" minlength="12" required autocomplete="new-password">
                <button type="submit" class="btn btn-primary">Générer la configuration</button>
            </form>
        <?php endif; ?>

    <?php else: ?>
        <h1>Connexion</h1>
        <?php if ($error !== null): ?><p class="alert alert-error" role="alert"><?= amstats_h($error) ?></p><?php endif; ?>
        <form method="post" class="auth-form">
            <input type="hidden" name="csrf" value="<?= amstats_h($csrf) ?>">
            <input type="hidden" name="action" value="login">
            <label for="password">Mot de passe</label>
            <input type="password" id="password" name="password" required autofocus autocomplete="current-password">
            <button type="submit" class="btn btn-primary">Se connecter</button>
        </form>
    <?php endif; ?>

    <p class="auth-foot">Accès réservé · AM Stats v<?= amstats_h($v) ?></p>
</main>
</body>
<?php else:
    $siteUrl = (string) ($config['site_url'] ?: ('https://' . ($config['allowed_hosts'][0] ?? '')));
    $siteHost = (string) (parse_url($siteUrl, PHP_URL_HOST) ?: $siteUrl);
    $today = (new DateTimeImmutable('today', new DateTimeZone($config['timezone'])))->format('Y-m-d');
?>
<body class="app"
      data-api="api.php"
      data-site="<?= amstats_h($siteUrl) ?>"
      data-today="<?= amstats_h($today) ?>"
      data-retention="<?= (int) $config['retention_days'] ?>">
<header class="topbar">
    <div class="topbar-inner">
        <div class="brand"><?= $logo ?><span class="brand-name">AM Stats</span><span class="brand-site"><?= amstats_h($siteHost) ?></span></div>
        <form method="post" class="logout-form">
            <input type="hidden" name="csrf" value="<?= amstats_h($csrf) ?>">
            <input type="hidden" name="action" value="logout">
            <button type="submit" class="btn btn-ghost">Déconnexion</button>
        </form>
    </div>
</header>

<main class="container" id="main" aria-busy="true">
    <?php if (!amstats_secret_ok($config)): ?>
        <p class="alert alert-error" role="alert">Le paramètre <code>secret_salt</code> de config.php est vide ou trop court (32 caractères minimum) : la collecte est désactivée.</p>
    <?php endif; ?>
    <p class="alert alert-error" id="alert" role="alert" hidden></p>

    <section class="toolbar" aria-label="Période analysée">
        <div class="segmented" role="group" aria-label="Choisir la période">
            <button type="button" class="seg" data-period="today" aria-pressed="false">Aujourd'hui</button>
            <button type="button" class="seg" data-period="yesterday" aria-pressed="false">Hier</button>
            <button type="button" class="seg" data-period="7d" aria-pressed="false">7 jours</button>
            <button type="button" class="seg" data-period="30d" aria-pressed="false">30 jours</button>
            <button type="button" class="seg" data-period="custom" aria-pressed="false">Personnalisé</button>
        </div>
        <form class="custom-range" id="custom-range" hidden>
            <label>Du <input type="date" id="range-from" required max="<?= amstats_h($today) ?>"></label>
            <label>au <input type="date" id="range-to" required max="<?= amstats_h($today) ?>"></label>
            <button type="submit" class="btn btn-primary">Appliquer</button>
        </form>
        <p class="period-label" id="period-label" aria-live="polite"></p>
    </section>

    <section class="kpis" aria-label="Indicateurs clés">
        <article class="kpi">
            <h2 class="kpi-title">Visiteurs uniques</h2>
            <p class="kpi-value" id="kpi-visitors">—</p>
            <p class="kpi-delta" id="kpi-visitors-delta"></p>
        </article>
        <article class="kpi">
            <h2 class="kpi-title">Pages vues</h2>
            <p class="kpi-value" id="kpi-pageviews">—</p>
            <p class="kpi-delta" id="kpi-pageviews-delta"></p>
        </article>
        <article class="kpi">
            <h2 class="kpi-title">Pages / visiteur</h2>
            <p class="kpi-value" id="kpi-ratio">—</p>
            <p class="kpi-delta" id="kpi-ratio-delta"></p>
        </article>
        <article class="kpi kpi-live">
            <h2 class="kpi-title"><span class="live-dot" aria-hidden="true"></span>Visiteurs actifs</h2>
            <p class="kpi-value" id="kpi-active">—</p>
            <p class="kpi-note">5 dernières minutes · <span id="live-updated">—</span></p>
        </article>
    </section>

    <section class="live-strip" id="live-strip" hidden aria-label="Pages consultées en ce moment">
        <span class="live-strip-title">En ce moment</span>
        <ul id="live-pages"></ul>
    </section>

    <section class="card">
        <div class="card-head">
            <h2>Évolution du trafic</h2>
            <ul class="legend" aria-label="Légende">
                <li><span class="swatch swatch-1" aria-hidden="true"></span>Pages vues</li>
                <li><span class="swatch swatch-2" aria-hidden="true"></span>Visiteurs uniques</li>
            </ul>
        </div>
        <div class="chart-box">
            <canvas id="traffic-chart" role="img" aria-label="Courbes des pages vues et des visiteurs uniques sur la période"></canvas>
        </div>
        <details class="data-details">
            <summary>Voir les données du graphique</summary>
            <div class="table-scroll">
                <table class="data-table" id="series-table">
                    <thead><tr><th scope="col" id="series-col">Période</th><th scope="col" class="num">Pages vues</th><th scope="col" class="num">Visiteurs uniques</th></tr></thead>
                    <tbody></tbody>
                </table>
            </div>
        </details>
    </section>

    <div class="grid">
        <section class="card card-pages">
            <div class="card-head">
                <h2>Pages les plus consultées</h2>
                <span class="card-meta" id="pages-meta"></span>
            </div>
            <table class="ranking" id="pages-table">
                <thead><tr><th scope="col">Page</th><th scope="col" class="num">Vues</th><th scope="col" class="num">Visiteurs</th></tr></thead>
                <tbody></tbody>
            </table>
        </section>

        <section class="card card-sources">
            <div class="card-head">
                <h2>Sources de trafic</h2>
                <span class="card-meta" id="sources-meta"></span>
            </div>
            <table class="ranking" id="sources-table">
                <thead><tr><th scope="col">Source</th><th scope="col" class="num">Visites</th><th scope="col" class="num">Part</th></tr></thead>
                <tbody></tbody>
            </table>
        </section>

        <section class="card card-devices">
            <div class="card-head">
                <h2>Appareils</h2>
                <span class="card-meta">visiteurs uniques</span>
            </div>
            <div class="stackbar" id="device-bar" role="img" aria-label="Répartition des visiteurs par type d'appareil"></div>
            <table class="ranking ranking-devices" id="devices-table">
                <thead><tr><th scope="col">Appareil</th><th scope="col" class="num">Visiteurs</th><th scope="col" class="num">Part</th></tr></thead>
                <tbody></tbody>
            </table>
        </section>
    </div>

    <footer class="foot">
        <p>Mesure d'audience sans cookie ni stockage sur l'appareil des visiteurs. Aucune adresse IP n'est enregistrée ; l'empreinte anonyme est renouvelée chaque jour et son sel détruit le lendemain. Données conservées <?= (int) $config['retention_days'] ?> jours.</p>
        <label class="optout"><input type="checkbox" id="optout"> Ne pas comptabiliser mes propres visites depuis ce navigateur</label>
        <p class="foot-meta" id="foot-meta"></p>
    </footer>
</main>

<script src="assets/vendor/chart.umd.min.js?v=4.5.1"></script>
<script src="assets/dashboard.js?v=<?= amstats_h($v) ?>"></script>
</body>
<?php endif; ?>
</html>
