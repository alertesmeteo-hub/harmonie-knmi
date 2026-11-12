<?php
/**
 * AM Stats — accès base de données (SQLite ou MySQL/MariaDB via PDO)
 * et création automatique du schéma (tables + index).
 */

declare(strict_types=1);

namespace AmStats;

if (!\defined('AMSTATS')) {
    \http_response_code(404);
    exit;
}

use PDO;
use PDOException;
use PDOStatement;
use RuntimeException;

final class Database
{
    private ?PDO $pdo = null;
    private string $driver;
    private string $prefix;
    private array $cfg;

    public function __construct(array $cfg)
    {
        $this->cfg = $cfg;
        $this->driver = ($cfg['driver'] ?? 'sqlite') === 'mysql' ? 'mysql' : 'sqlite';
        $prefix = (string) ($cfg['table_prefix'] ?? 'am_');
        if (!preg_match('/^[A-Za-z0-9_]{0,20}$/', $prefix)) {
            throw new RuntimeException('Préfixe de table invalide (lettres, chiffres et _ uniquement).');
        }
        $this->prefix = $prefix;
    }

    public function driver(): string
    {
        return $this->driver;
    }

    public function table(string $name): string
    {
        return $this->prefix . $name;
    }

    /**
     * Remplace {pageviews}, {paths}, {salts}, {logins} par les noms réels des tables.
     */
    public function sql(string $sql): string
    {
        return strtr($sql, [
            '{pageviews}' => $this->table('pageviews'),
            '{paths}'     => $this->table('paths'),
            '{salts}'     => $this->table('salts'),
            '{logins}'    => $this->table('logins'),
        ]);
    }

    public function insertIgnore(): string
    {
        return $this->driver === 'sqlite' ? 'INSERT OR IGNORE' : 'INSERT IGNORE';
    }

    public function pdo(): PDO
    {
        if ($this->pdo === null) {
            $this->pdo = $this->connect();
        }
        return $this->pdo;
    }

    /**
     * Requête préparée. Les entiers sont liés en PARAM_INT : indispensable sous
     * MySQL pour comparer exactement les empreintes 64 bits (sinon conversion en flottant).
     */
    public function run(string $sql, array $params = []): PDOStatement
    {
        $stmt = $this->pdo()->prepare($this->sql($sql));
        foreach ($params as $name => $value) {
            if (is_int($value)) {
                $type = PDO::PARAM_INT;
            } elseif ($value === null) {
                $type = PDO::PARAM_NULL;
            } else {
                $type = PDO::PARAM_STR;
                $value = (string) $value;
            }
            $stmt->bindValue(is_int($name) ? $name + 1 : $name, $value, $type);
        }
        $stmt->execute();
        return $stmt;
    }

    private function connect(): PDO
    {
        $options = [
            PDO::ATTR_ERRMODE            => PDO::ERRMODE_EXCEPTION,
            PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
        ];

        if ($this->driver === 'sqlite') {
            $path = (string) $this->cfg['sqlite_path'];
            $dir = dirname($path);
            if (!is_dir($dir) && !@mkdir($dir, 0750, true) && !is_dir($dir)) {
                throw new RuntimeException('Impossible de créer le dossier de données : ' . $dir);
            }
            $options[PDO::ATTR_TIMEOUT] = 5;
            $pdo = new PDO('sqlite:' . $path, null, null, $options);
            $pdo->exec('PRAGMA busy_timeout = 5000');
            $pdo->exec('PRAGMA synchronous = NORMAL');
            $pdo->exec('PRAGMA temp_store = MEMORY');
            return $pdo;
        }

        $m = $this->cfg['mysql'];
        if (!empty($m['socket'])) {
            $dsn = sprintf('mysql:unix_socket=%s;dbname=%s;charset=utf8mb4', $m['socket'], $m['dbname']);
        } else {
            $dsn = sprintf('mysql:host=%s;port=%d;dbname=%s;charset=utf8mb4', $m['host'], (int) $m['port'], $m['dbname']);
        }
        $options[PDO::ATTR_EMULATE_PREPARES] = false;
        return new PDO($dsn, (string) $m['user'], (string) $m['password'], $options);
    }

    public static function isMissingTable(PDOException $e): bool
    {
        $info = $e->errorInfo ?? [];
        return ($info[0] ?? '') === '42S02'
            || (int) ($info[1] ?? 0) === 1146
            || stripos($e->getMessage(), 'no such table') !== false;
    }

    /**
     * Exécute $fn ; si une table manque (première utilisation), crée le schéma
     * puis réessaie une seule fois. Aucun surcoût en fonctionnement normal.
     *
     * @template T
     * @param callable():T $fn
     * @return T
     */
    public function withSchema(callable $fn)
    {
        try {
            return $fn();
        } catch (PDOException $e) {
            if (!self::isMissingTable($e)) {
                throw $e;
            }
            $this->createSchema();
            return $fn();
        }
    }

    /**
     * Vérifie que les quatre tables existent, sinon les crée.
     */
    public function ensureSchema(): void
    {
        $this->withSchema(function (): void {
            $this->run('SELECT (SELECT 1 FROM {pageviews} LIMIT 1) AS a, (SELECT 1 FROM {paths} LIMIT 1) AS b,
                               (SELECT 1 FROM {salts} LIMIT 1) AS c, (SELECT 1 FROM {logins} LIMIT 1) AS d');
        });
    }

    public function createSchema(): void
    {
        $pdo = $this->pdo();
        foreach ($this->schemaStatements() as $statement) {
            $pdo->exec($this->sql($statement));
        }
    }

    /**
     * Schéma compact (≈ 4 fois moins de place qu'un stockage « texte ») :
     *  - {pageviews} : une ligne par page vue, uniquement des entiers.
     *      day      AAAAMMJJ (ex. 20261112), heure locale 0-23
     *      visitor  empreinte anonyme du jour (64 bits d'un HMAC-SHA256)
     *      path_id  renvoie au dictionnaire {paths}
     *      source   renseignée sur la première page d'une visite seulement (NULL sinon)
     *      device   1 = mobile, 2 = tablette, 3 = ordinateur
     *  - {paths}  : dictionnaire des chemins d'URL (chaque URL stockée une seule fois).
     *  - {salts}  : sel aléatoire du jour, détruit dès le lendemain.
     *  - {logins} : anti-force brute du tableau de bord (clé = HMAC de l'IP).
     *
     * Les index sont « couvrants » : les requêtes du tableau de bord lisent les
     * index sans parcourir la table.
     *
     * @return string[]
     */
    private function schemaStatements(): array
    {
        $p = $this->prefix;

        if ($this->driver === 'sqlite') {
            return [
                'PRAGMA journal_mode = WAL',
                'CREATE TABLE IF NOT EXISTS {pageviews} (
                    id       INTEGER PRIMARY KEY,
                    ts       INTEGER NOT NULL,
                    day      INTEGER NOT NULL,
                    hour     INTEGER NOT NULL,
                    visitor  INTEGER NOT NULL,
                    path_id  INTEGER NOT NULL,
                    source   TEXT    NULL,
                    device   INTEGER NOT NULL
                )',
                "CREATE INDEX IF NOT EXISTS {$p}pv_day_hour   ON {pageviews} (day, hour, visitor)",
                "CREATE INDEX IF NOT EXISTS {$p}pv_day_path   ON {pageviews} (day, path_id, visitor)",
                "CREATE INDEX IF NOT EXISTS {$p}pv_day_source ON {pageviews} (day, source) WHERE source IS NOT NULL",
                "CREATE INDEX IF NOT EXISTS {$p}pv_day_device ON {pageviews} (day, device, visitor)",
                "CREATE INDEX IF NOT EXISTS {$p}pv_visitor_ts ON {pageviews} (visitor, ts)",
                "CREATE INDEX IF NOT EXISTS {$p}pv_ts         ON {pageviews} (ts)",
                'CREATE TABLE IF NOT EXISTS {paths} (
                    id   INTEGER PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE
                )',
                'CREATE TABLE IF NOT EXISTS {salts} (
                    day  INTEGER PRIMARY KEY,
                    salt TEXT NOT NULL
                )',
                'CREATE TABLE IF NOT EXISTS {logins} (
                    id           TEXT PRIMARY KEY,
                    attempts     INTEGER NOT NULL DEFAULT 0,
                    first_at     INTEGER NOT NULL,
                    locked_until INTEGER NOT NULL DEFAULT 0
                ) WITHOUT ROWID',
            ];
        }

        return [
            'CREATE TABLE IF NOT EXISTS {pageviews} (
                id      BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                ts      INT UNSIGNED NOT NULL,
                day     INT UNSIGNED NOT NULL,
                hour    TINYINT UNSIGNED NOT NULL,
                visitor BIGINT NOT NULL,
                path_id INT UNSIGNED NOT NULL,
                source  VARCHAR(190) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL,
                device  TINYINT UNSIGNED NOT NULL,
                PRIMARY KEY (id),
                KEY idx_day_hour   (day, hour, visitor),
                KEY idx_day_path   (day, path_id, visitor),
                KEY idx_day_source (day, source),
                KEY idx_day_device (day, device, visitor),
                KEY idx_visitor_ts (visitor, ts),
                KEY idx_ts         (ts)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci',
            'CREATE TABLE IF NOT EXISTS {paths} (
                id   INT UNSIGNED NOT NULL AUTO_INCREMENT,
                path VARCHAR(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
                PRIMARY KEY (id),
                UNIQUE KEY uniq_path (path)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC',
            'CREATE TABLE IF NOT EXISTS {salts} (
                day  INT UNSIGNED NOT NULL,
                salt CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
                PRIMARY KEY (day)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4',
            'CREATE TABLE IF NOT EXISTS {logins} (
                id           CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
                attempts     INT UNSIGNED NOT NULL DEFAULT 0,
                first_at     INT UNSIGNED NOT NULL,
                locked_until INT UNSIGNED NOT NULL DEFAULT 0,
                PRIMARY KEY (id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4',
        ];
    }
}
