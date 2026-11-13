<?php
/**
 * AM Stats — normalisation des sources de trafic.
 *
 * Seul le nom de domaine du référent est conservé (jamais l'URL complète,
 * qui peut contenir des termes de recherche ou des identifiants).
 */

declare(strict_types=1);

namespace AmStats;

if (!\defined('AMSTATS')) {
    \http_response_code(404);
    exit;
}

final class Referrer
{
    public const DIRECT = '(direct)';

    /** Règles évaluées dans l'ordre : la première qui correspond l'emporte. */
    private const RULES = [
        '~(^|\.)gemini\.google\.com$~'                                      => 'Gemini',
        '~(^|\.)mail\.google\.com$|^com\.google\.android\.gm$~'             => 'Gmail',
        '~(^|\.)news\.google\.[a-z.]+$|^com\.google\.android\.apps\.magazines$~' => 'Google Actualités',
        '~(^|\.)google\.[a-z]{2,3}(\.[a-z]{2})?$|^com\.google\.android\.googlequicksearchbox$~' => 'Google',
        '~(^|\.)bing\.com$~'                                                => 'Bing',
        '~(^|\.)copilot\.microsoft\.com$~'                                  => 'Copilot',
        '~(^|\.)duckduckgo\.com$~'                                          => 'DuckDuckGo',
        '~(^|\.)qwant\.com$~'                                               => 'Qwant',
        '~(^|\.)ecosia\.org$~'                                              => 'Ecosia',
        '~(^|\.)search\.brave\.com$~'                                       => 'Brave Search',
        '~(^|\.)startpage\.com$~'                                           => 'Startpage',
        '~(^|\.)lilo\.org$~'                                                => 'Lilo',
        '~(^|\.)yahoo\.[a-z.]+$~'                                           => 'Yahoo',
        '~(^|\.)yandex\.[a-z]{2,3}$~'                                       => 'Yandex',
        '~(^|\.)(facebook\.com|fb\.com|fb\.me)$|^com\.facebook\.~'          => 'Facebook',
        '~(^|\.)instagram\.com$|^com\.instagram\.~'                         => 'Instagram',
        '~(^|\.)(t\.co|twitter\.com|x\.com)$~'                              => 'X (Twitter)',
        '~(^|\.)(linkedin\.com|lnkd\.in)$|^com\.linkedin\.~'                => 'LinkedIn',
        '~(^|\.)(youtube\.com|youtu\.be)$~'                                 => 'YouTube',
        '~(^|\.)reddit\.com$~'                                              => 'Reddit',
        '~(^|\.)pinterest\.[a-z.]+$|^pin\.it$~'                             => 'Pinterest',
        '~(^|\.)tiktok\.com$~'                                              => 'TikTok',
        '~(^|\.)bsky\.app$~'                                                => 'Bluesky',
        '~(^|\.)threads\.(net|com)$~'                                       => 'Threads',
        '~(^|\.)(whatsapp\.com|wa\.me)$~'                                   => 'WhatsApp',
        '~(^|\.)(t\.me|telegram\.org)$|^org\.telegram\.~'                   => 'Telegram',
        '~(^|\.)(chatgpt\.com|chat\.openai\.com)$~'                         => 'ChatGPT',
        '~(^|\.)perplexity\.ai$~'                                           => 'Perplexity',
        '~(^|\.)claude\.ai$~'                                               => 'Claude',
        '~(^|\.)msn\.com$~'                                                 => 'MSN',
        '~(^|\.)wikipedia\.org$~'                                           => 'Wikipédia',
        '~(^|\.)(outlook\.live\.com|outlook\.office\.com|outlook\.office365\.com)$~' => 'Outlook',
    ];

    /** Valeurs courantes de utm_source / ref ramenées à un libellé unique. */
    private const UTM_ALIASES = [
        'fb'         => 'Facebook',
        'facebook'   => 'Facebook',
        'ig'         => 'Instagram',
        'instagram'  => 'Instagram',
        'twitter'    => 'X (Twitter)',
        'x'          => 'X (Twitter)',
        'linkedin'   => 'LinkedIn',
        'youtube'    => 'YouTube',
        'google'     => 'Google',
        'bing'       => 'Bing',
        'whatsapp'   => 'WhatsApp',
        'telegram'   => 'Telegram',
        'bluesky'    => 'Bluesky',
        'threads'    => 'Threads',
        'tiktok'     => 'TikTok',
        'pinterest'  => 'Pinterest',
        'chatgpt'    => 'ChatGPT',
        'chatgpt.com'=> 'ChatGPT',
        'newsletter' => 'Newsletter',
        'email'      => 'E-mail',
        'mail'       => 'E-mail',
    ];

    /**
     * Détermine la source d'une visite.
     *
     * @param string   $referrer  document.referrer transmis par la sonde
     * @param string   $utm       utm_source / ref / source présent dans l'URL
     * @param string[] $ownHosts  domaines du site (navigation interne = accès direct)
     */
    public static function source(string $referrer, string $utm, array $ownHosts): string
    {
        $utm = trim($utm);
        if ($utm !== '') {
            return self::fromUtm($utm);
        }

        $referrer = trim($referrer);
        if ($referrer === '') {
            return self::DIRECT;
        }

        $host = parse_url($referrer, PHP_URL_HOST);
        if (!is_string($host) || $host === '') {
            return self::DIRECT;
        }

        $host = strtolower(rtrim($host, '.'));
        if (in_array($host, $ownHosts, true)) {
            return self::DIRECT;
        }

        return self::classifyHost($host);
    }

    public static function classifyHost(string $host): string
    {
        $host = strtolower($host);
        // www., m., l., lm., mobile., amp., touch. → domaine principal
        $host = (string) preg_replace('~^(?:(?:www\d?|m|l|lm|mobile|amp|touch)\.)+(?=[^.]+\.[^.]+)~', '', $host);

        foreach (self::RULES as $pattern => $label) {
            if (preg_match($pattern, $host)) {
                return $label;
            }
        }

        return amstats_truncate($host, 190);
    }

    private static function fromUtm(string $utm): string
    {
        $clean = strtolower((string) preg_replace('~[^\p{L}\p{N}._\- ]+~u', '', $utm));
        $clean = trim(amstats_truncate($clean, 60));
        if ($clean === '') {
            return self::DIRECT;
        }
        if (isset(self::UTM_ALIASES[$clean])) {
            return self::UTM_ALIASES[$clean];
        }
        if (strpos($clean, '.') !== false) {
            return self::classifyHost($clean);
        }
        return $clean;
    }
}
