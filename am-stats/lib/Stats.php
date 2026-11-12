<?php
/**
 * AM Stats — requêtes d'agrégation pour le tableau de bord.
 */

declare(strict_types=1);

namespace AmStats;

if (!\defined('AMSTATS')) {
    \http_response_code(404);
    exit;
}

use DateTimeImmutable;
use DateTimeZone;
use InvalidArgumentException;

final class Stats
{
    public const PERIODS = ['today', 'yesterday', '7d', '30d', 'custom'];
    private const MAX_DAYS = 731;

    private Database $db;
    private array $config;
    private DateTimeZone $tz;

    public function __construct(Database $db, array $config)
    {
        $this->db = $db;
        $this->config = $config;
        $this->tz = new DateTimeZone($config['timezone']);
    }

    /**
     * Traduit un choix de période en bornes [from, to] (dates locales incluses).
     */
    public static function resolvePeriod(string $period, ?string $from, ?string $to, string $timezone): array
    {
        $tz = new DateTimeZone($timezone);
        $today = new DateTimeImmutable('today', $tz);

        switch ($period) {
            case 'today':
                $f = $t = $today;
                break;
            case 'yesterday':
                $f = $t = $today->modify('-1 day');
                break;
            case '7d':
                $f = $today->modify('-6 days');
                $t = $today;
                break;
            case '30d':
                $f = $today->modify('-29 days');
                $t = $today;
                break;
            case 'custom':
                $f = self::parseDate($from, $tz);
                $t = self::parseDate($to, $tz);
                if ($f === null || $t === null) {
                    throw new InvalidArgumentException('Dates invalides : format attendu AAAA-MM-JJ.');
                }
                if ($f > $t) {
                    [$f, $t] = [$t, $f];
                }
                if ($t > $today) {
                    $t = $today;
                }
                if ($f > $today) {
                    $f = $today;
                }
                if ($f->diff($t)->days + 1 > self::MAX_DAYS) {
                    throw new InvalidArgumentException('Période limitée à ' . self::MAX_DAYS . ' jours.');
                }
                break;
            default:
                throw new InvalidArgumentException('Période inconnue.');
        }

        return [
            'period' => $period,
            'from'   => $f->format('Y-m-d'),
            'to'     => $t->format('Y-m-d'),
            'days'   => $f->diff($t)->days + 1,
        ];
    }

    /** « 2026-11-12 » → 20261112 (format de stockage). */
    private static function dayInt(string $ymd): int
    {
        return (int) str_replace('-', '', $ymd);
    }

    /** 20261112 → « 2026-11-12 ». */
    private static function dayString($day): string
    {
        $d = str_pad((string) (int) $day, 8, '0', STR_PAD_LEFT);
        return substr($d, 0, 4) . '-' . substr($d, 4, 2) . '-' . substr($d, 6, 2);
    }

    private static function parseDate(?string $value, DateTimeZone $tz): ?DateTimeImmutable
    {
        if (!is_string($value) || !preg_match('/^\d{4}-\d{2}-\d{2}$/', $value)) {
            return null;
        }
        $date = DateTimeImmutable::createFromFormat('!Y-m-d', $value, $tz);
        return ($date && $date->format('Y-m-d') === $value) ? $date : null;
    }

    /**
     * Toutes les données d'une période en un seul appel.
     */
    public function summary(array $range, int $limit = 20): array
    {
        $started = microtime(true);
        $from = $range['from'];
        $to = $range['to'];
        $days = (int) $range['days'];

        $now = new DateTimeImmutable('now', $this->tz);
        $today = $now->format('Y-m-d');

        // Période précédente de même durée. Si la période en cours inclut
        // aujourd'hui (journée incomplète), la comparaison s'arrête à la même heure.
        $prevTo = (new DateTimeImmutable($from, $this->tz))->modify('-1 day');
        $prevFrom = $prevTo->modify('-' . ($days - 1) . ' days');
        $cap = null;
        if ($to === $today) {
            $cap = [
                'day'  => (int) $now->modify('-' . $days . ' days')->format('Ymd'),
                'hour' => (int) $now->format('G'),
            ];
        }

        $series = $days === 1 ? $this->hourly($from, $today, (int) $now->format('G')) : $this->daily($from, $to);
        // Sur plusieurs jours, les totaux se déduisent de la série quotidienne
        // (empreintes journalières ⇒ somme des visiteurs uniques de chaque jour) : une requête de moins.
        $totals = $days === 1 ? $this->totals($from, $to) : $this->totalsFromSeries($series, $from, $to);
        $previous = $this->totals($prevFrom->format('Y-m-d'), $prevTo->format('Y-m-d'), $cap);

        return [
            'range' => [
                'period'      => $range['period'],
                'from'        => $from,
                'to'          => $to,
                'days'        => $days,
                'granularity' => $days === 1 ? 'hour' : 'day',
                'previous'    => [
                    'from'       => $prevFrom->format('Y-m-d'),
                    'to'         => $prevTo->format('Y-m-d'),
                    'same_hour'  => $cap !== null,
                ],
            ],
            'totals'   => $totals,
            'previous' => $previous,
            'series'   => $series,
            'pages'    => $this->topPages($from, $to, $limit),
            'sources'  => $this->sources($from, $to, $limit),
            'devices'  => $this->devices($from, $to),
            'realtime' => $this->realtime(),
            'meta'     => [
                'generated_at' => $now->format(DATE_ATOM),
                'query_ms'     => round((microtime(true) - $started) * 1000, 1),
                'timezone'     => $this->config['timezone'],
            ],
        ];
    }

    /**
     * Visiteurs uniques, pages vues, visites et ratio pages/visiteur.
     *
     * L'empreinte changeant chaque jour, un visiteur présent 3 jours compte 3 fois
     * sur une période de 7 jours (somme des visiteurs uniques quotidiens).
     */
    public function totals(string $from, string $to, ?array $cap = null): array
    {
        $where = 'day BETWEEN :from AND :to';
        $params = [':from' => self::dayInt($from), ':to' => self::dayInt($to)];
        if ($cap !== null) {
            $where .= ' AND (day < :cap_day OR (day = :cap_day2 AND hour <= :cap_hour))';
            $params += [':cap_day' => $cap['day'], ':cap_day2' => $cap['day'], ':cap_hour' => $cap['hour']];
        }

        // Dénombrement jour par jour puis somme : résultat identique (l'empreinte
        // change chaque jour) et 2 à 3 fois plus rapide qu'un COUNT(DISTINCT) global.
        $row = $this->db->run(
            "SELECT SUM(pv) AS pageviews, SUM(v) AS visitors
               FROM (SELECT COUNT(*) AS pv, COUNT(DISTINCT visitor) AS v
                       FROM {pageviews} WHERE $where GROUP BY day) d",
            $params
        )->fetch() ?: [];

        return $this->withRatio((int) ($row['pageviews'] ?? 0), (int) ($row['visitors'] ?? 0), $this->visits($where, $params));
    }

    private function totalsFromSeries(array $series, string $from, string $to): array
    {
        return $this->withRatio(
            (int) array_sum($series['pageviews']),
            (int) array_sum($series['visitors']),
            $this->visits('day BETWEEN :from AND :to', [':from' => self::dayInt($from), ':to' => self::dayInt($to)])
        );
    }

    /** Nombre de visites = nombre de pages d'entrée (source renseignée). */
    private function visits(string $where, array $params): int
    {
        return (int) $this->db->run(
            "SELECT COUNT(*) FROM {pageviews} WHERE $where AND source IS NOT NULL",
            $params
        )->fetchColumn();
    }

    private function withRatio(int $pageviews, int $visitors, int $visits): array
    {
        return [
            'visitors'  => $visitors,
            'pageviews' => $pageviews,
            'visits'    => $visits,
            'ratio'     => $visitors > 0 ? round($pageviews / $visitors, 2) : 0.0,
        ];
    }

    /** Série horaire (0 h → 23 h) ; les heures futures d'aujourd'hui valent null. */
    private function hourly(string $day, string $today, int $currentHour): array
    {
        $rows = $this->db->run(
            'SELECT hour, COUNT(*) AS pageviews, COUNT(DISTINCT visitor) AS visitors
               FROM {pageviews} WHERE day = :day GROUP BY hour',
            [':day' => self::dayInt($day)]
        )->fetchAll();

        $byHour = [];
        foreach ($rows as $r) {
            $byHour[(int) $r['hour']] = $r;
        }

        $labels = $pageviews = $visitors = [];
        for ($h = 0; $h < 24; $h++) {
            $labels[] = $h;
            $future = $day === $today && $h > $currentHour;
            $pageviews[] = $future ? null : (int) ($byHour[$h]['pageviews'] ?? 0);
            $visitors[] = $future ? null : (int) ($byHour[$h]['visitors'] ?? 0);
        }

        return ['labels' => $labels, 'pageviews' => $pageviews, 'visitors' => $visitors];
    }

    /** Série journalière, jours sans visite complétés par des zéros. */
    private function daily(string $from, string $to): array
    {
        $rows = $this->db->run(
            'SELECT day, COUNT(*) AS pageviews, COUNT(DISTINCT visitor) AS visitors
               FROM {pageviews} WHERE day BETWEEN :from AND :to GROUP BY day',
            [':from' => self::dayInt($from), ':to' => self::dayInt($to)]
        )->fetchAll();

        $byDay = [];
        foreach ($rows as $r) {
            $byDay[self::dayString($r['day'])] = $r;
        }

        $labels = $pageviews = $visitors = [];
        $cursor = new DateTimeImmutable($from, $this->tz);
        $end = new DateTimeImmutable($to, $this->tz);
        while ($cursor <= $end) {
            $key = $cursor->format('Y-m-d');
            $labels[] = $key;
            $pageviews[] = (int) ($byDay[$key]['pageviews'] ?? 0);
            $visitors[] = (int) ($byDay[$key]['visitors'] ?? 0);
            $cursor = $cursor->modify('+1 day');
        }

        return ['labels' => $labels, 'pageviews' => $pageviews, 'visitors' => $visitors];
    }

    private function topPages(string $from, string $to, int $limit): array
    {
        $limit = max(1, min(100, $limit));
        $rows = $this->db->run(
            "SELECT p.path, t.pageviews, t.visitors
               FROM (SELECT path_id, SUM(pv) AS pageviews, SUM(v) AS visitors
                       FROM (SELECT day, path_id, COUNT(*) AS pv, COUNT(DISTINCT visitor) AS v
                               FROM {pageviews} WHERE day BETWEEN :from AND :to
                              GROUP BY day, path_id) d
                      GROUP BY path_id ORDER BY pageviews DESC, path_id ASC LIMIT $limit) t
               JOIN {paths} p ON p.id = t.path_id
              ORDER BY t.pageviews DESC, p.path ASC",
            [':from' => self::dayInt($from), ':to' => self::dayInt($to)]
        )->fetchAll();

        return array_map(static fn (array $r): array => [
            'path'      => (string) $r['path'],
            'pageviews' => (int) $r['pageviews'],
            'visitors'  => (int) $r['visitors'],
        ], $rows);
    }

    private function sources(string $from, string $to, int $limit): array
    {
        $limit = max(1, min(100, $limit));
        $rows = $this->db->run(
            "SELECT source, COUNT(*) AS visits
               FROM {pageviews} WHERE day BETWEEN :from AND :to AND source IS NOT NULL
              GROUP BY source ORDER BY visits DESC, source ASC LIMIT $limit",
            [':from' => self::dayInt($from), ':to' => self::dayInt($to)]
        )->fetchAll();

        return array_map(static fn (array $r): array => [
            'source' => (string) $r['source'],
            'visits' => (int) $r['visits'],
        ], $rows);
    }

    private function devices(string $from, string $to): array
    {
        $rows = $this->db->run(
            'SELECT device, SUM(v) AS visitors, SUM(pv) AS pageviews
               FROM (SELECT day, device, COUNT(DISTINCT visitor) AS v, COUNT(*) AS pv
                       FROM {pageviews} WHERE day BETWEEN :from AND :to
                      GROUP BY day, device) d
              GROUP BY device',
            [':from' => self::dayInt($from), ':to' => self::dayInt($to)]
        )->fetchAll();

        $names = array_flip(Collector::DEVICES);
        $out = [];
        foreach (Collector::DEVICES as $name => $code) {
            $out[$name] = ['device' => $name, 'visitors' => 0, 'pageviews' => 0];
        }
        foreach ($rows as $r) {
            $name = $names[(int) $r['device']] ?? null;
            if ($name !== null) {
                $out[$name]['visitors'] = (int) $r['visitors'];
                $out[$name]['pageviews'] = (int) $r['pageviews'];
            }
        }
        return array_values($out);
    }

    /**
     * Visiteurs actifs sur les $seconds dernières secondes + pages consultées.
     */
    public function realtime(int $seconds = 300): array
    {
        $since = time() - $seconds;
        $active = (int) $this->db->run(
            'SELECT COUNT(DISTINCT visitor) FROM {pageviews} WHERE ts >= :since',
            [':since' => $since]
        )->fetchColumn();

        // Agrégation d'abord (index sur ts), jointure au dictionnaire ensuite :
        // l'ordre inverse obligerait SQLite à parcourir toute la table.
        $pages = $this->db->run(
            'SELECT p.path, t.visitors
               FROM (SELECT path_id, COUNT(DISTINCT visitor) AS visitors
                       FROM {pageviews} WHERE ts >= :since
                      GROUP BY path_id ORDER BY visitors DESC LIMIT 5) t
               JOIN {paths} p ON p.id = t.path_id
              ORDER BY t.visitors DESC, p.path ASC',
            [':since' => $since]
        )->fetchAll();

        return [
            'active'  => $active,
            'window'  => $seconds,
            'pages'   => array_map(static fn (array $r): array => [
                'path'     => (string) $r['path'],
                'visitors' => (int) $r['visitors'],
            ], $pages),
        ];
    }
}
