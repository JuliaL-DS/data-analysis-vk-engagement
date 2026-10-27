-- =============================================================================
-- fact_vk_posts_ch.sql (ClickHouse)
-- Аналог fact_vk_posts.sql под колоночную СУБД.
--
-- Ключевые отличия от PostgreSQL-версии:
--   * вместо COALESCE — ifNull;
--   * вместо NULLIF — if(views = 0, 0, ...);
--   * вместо LAG — lagInFrame (оконная функция);
--   * вместо regex ~* — match(str, '(?i)...') на движке RE2.
--
-- Предполагаемая схема source `{{ source('vk', 'vk_posts_raw') }}`:
--   post_id      UInt64
--   post_date    DateTime
--   text         String
--   likes        Int32
--   comments     Int32
--   reposts      Int32
--   views        Int32
--   is_deleted   UInt8
--
-- Границы применимости:
--   1) Регулярки рубрик зеркалят RUBRIC_PATTERNS из main.py.
--      \b — word boundary, поддерживается движком RE2.
--   2) text_length и флаги вложений НЕ считаем — см. комментарии
--      в fact_vk_posts.sql и README.
-- =============================================================================

WITH source AS (
    SELECT
        post_id,
        post_date,
        text,
        likes,
        comments,
        reposts,
        views,
        is_deleted
    FROM {{ source('vk', 'vk_posts_raw') }}
),

-- Флаги рубрик через match() на RE2 (case-insensitive флаг (?i)).
with_rubrics AS (
    SELECT
        *,
        toUInt8(match(ifNull(text, ''), '(?i)уроки\\s+истори'))             AS rubric_uroki_istorii,
        toUInt8(match(ifNull(text, ''), '(?i)серийный\\s+номер'))            AS rubric_seriynyi_nomer,
        toUInt8(match(ifNull(text, ''), '(?i)вторая\\s+мировая'))            AS rubric_vtoraya_mirovaya,
        toUInt8(match(ifNull(text, ''), '(?i)простовещи|простые\\s+вещи'))   AS rubric_prostoveshi,
        toUInt8(match(ifNull(text, ''), '(?i)долецкая'))                     AS rubric_doletskaya,
        toUInt8(match(ifNull(text, ''), '(?i)\\bличности\\b'))               AS rubric_lichnosti,
        -- v2: добавлено по результатам ручного аудита заголовков.
        toUInt8(match(ifNull(text, ''), '(?i)\\bчтиво\\b'))                  AS rubric_chtivo,
        toUInt8(match(ifNull(text, ''), '(?i)истори[ия]\\s+кумиров'))         AS rubric_istorii_kumirov,
        toUInt8(match(ifNull(text, ''), '(?i)истори[ия]\\s+миллиардов'))      AS rubric_istorii_milliardov,
        toUInt8(match(ifNull(text, ''), '(?i)истори[яи]\\s+бренда'))          AS rubric_istoriya_brenda
    FROM source
),

-- Интервал до предыдущего поста (в днях) через lagInFrame.
with_interval AS (
    SELECT
        *,
        dateDiff(
            'second',
            lagInFrame(post_date) OVER (ORDER BY post_date ROWS BETWEEN 1 PRECEDING AND CURRENT ROW),
            post_date
        ) / 86400.0 AS days_since_previous_post
    FROM with_rubrics
),

cleaned AS (
    SELECT
        post_id,
        post_date,
        ifNull(text, '')                        AS text,
        ifNull(likes, 0)                        AS likes,
        ifNull(comments, 0)                     AS comments,
        ifNull(reposts, 0)                      AS reposts,
        ifNull(views, 0)                        AS views,
        ifNull(is_deleted, 0)                   AS is_deleted,
        ifNull(rubric_uroki_istorii, 0)         AS rubric_uroki_istorii,
        ifNull(rubric_seriynyi_nomer, 0)        AS rubric_seriynyi_nomer,
        ifNull(rubric_vtoraya_mirovaya, 0)      AS rubric_vtoraya_mirovaya,
        ifNull(rubric_prostoveshi, 0)           AS rubric_prostoveshi,
        ifNull(rubric_doletskaya, 0)            AS rubric_doletskaya,
        ifNull(rubric_lichnosti, 0)             AS rubric_lichnosti,
        ifNull(rubric_chtivo, 0)                AS rubric_chtivo,
        ifNull(rubric_istorii_kumirov, 0)       AS rubric_istorii_kumirov,
        ifNull(rubric_istorii_milliardov, 0)    AS rubric_istorii_milliardov,
        ifNull(rubric_istoriya_brenda, 0)       AS rubric_istoriya_brenda,
        days_since_previous_post
    FROM with_interval
)

SELECT
    post_id,
    post_date,
    text,
    likes,
    comments,
    reposts,
    views,
    likes + comments + reposts             AS total_engagement,
    if(views = 0, 0, round((likes + comments + reposts) / views, 4)) AS engagement_rate,
    is_deleted,
    rubric_uroki_istorii,
    rubric_seriynyi_nomer,
    rubric_vtoraya_mirovaya,
    rubric_prostoveshi,
    rubric_doletskaya,
    rubric_lichnosti,
    rubric_chtivo,
    rubric_istorii_kumirov,
    rubric_istorii_milliardov,
    rubric_istoriya_brenda,
    days_since_previous_post,
    toHour(post_date)                      AS hour,
    toDayOfWeek(post_date)                 AS weekday,
    multiIf(
        toDayOfWeek(post_date) = 1, 'Пн',
        toDayOfWeek(post_date) = 2, 'Вт',
        toDayOfWeek(post_date) = 3, 'Ср',
        toDayOfWeek(post_date) = 4, 'Чт',
        toDayOfWeek(post_date) = 5, 'Пт',
        toDayOfWeek(post_date) = 6, 'Сб',
        'Вс'
    )                                      AS weekday_ru
FROM cleaned
WHERE is_deleted = 0