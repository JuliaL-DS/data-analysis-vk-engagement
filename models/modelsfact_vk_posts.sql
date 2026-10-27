-- =============================================================================
-- fact_vk_posts.sql (PostgreSQL)
-- Витрина фактов по постам паблика minaev_live.
--
-- Модель повторяет логику Python-пайплайна main.py:
--   * нормализация реакций (likes, comments, reposts, views) с COALESCE;
--   * флаги 10 рубрик через regex по полю text;
--   * интервал между постами (days_since_previous_post) через LAG;
--   * ER (engagement_rate) с защитой от деления на ноль;
--   * календарные признаки (hour, weekday).
--
-- Предполагаемая схема source `{{ source('vk', 'vk_posts_raw') }}`:
--   post_id      BIGINT
--   post_date    TIMESTAMPTZ
--   text         TEXT
--   likes        INT
--   comments     INT
--   reposts      INT
--   views        INT
--   is_deleted   BOOLEAN
--
-- Границы применимости:
--   1) Модель НЕ делает ingestion. Сырые данные должны быть загружены
--      в staging отдельно (python-скрипт main.py или dlt / airbyte).
--   2) POSIX-регекс (~*) — специфика PostgreSQL. Для ClickHouse
--      используйте fact_vk_posts_ch.sql.
--   3) Регулярки рубрик зеркалят RUBRIC_PATTERNS из main.py.
--      Word boundary \y — синтаксис PostgreSQL ARE (аналог \b в Python).
--   4) text_length НЕ считаем — у видео-постов это длина заголовка, у
--      лонгридов — реального текста; смешивать нельзя (см. README).
--   5) Флаги вложений (has_photo / has_poll / has_video) НЕ считаем —
--      они непригодны для анализа из-за дисбаланса классов (см. README).
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

-- Флаги рубрик через POSIX-регексы (case-insensitive, ~*).
rubrics AS (
    SELECT
        post_id,
        CASE WHEN COALESCE(text, '') ~* 'уроки\s+истори'                   THEN 1 ELSE 0 END AS rubric_uroki_istorii,
        CASE WHEN COALESCE(text, '') ~* 'серийный\s+номер'                  THEN 1 ELSE 0 END AS rubric_seriynyi_nomer,
        CASE WHEN COALESCE(text, '') ~* 'вторая\s+мировая'                  THEN 1 ELSE 0 END AS rubric_vtoraya_mirovaya,
        CASE WHEN COALESCE(text, '') ~* '(простовещи|простые\s+вещи)'       THEN 1 ELSE 0 END AS rubric_prostoveshi,
        CASE WHEN COALESCE(text, '') ~* 'долецкая'                          THEN 1 ELSE 0 END AS rubric_doletskaya,
        CASE WHEN COALESCE(text, '') ~* '\yличности\y'                      THEN 1 ELSE 0 END AS rubric_lichnosti,
        -- v2: добавлено по результатам ручного аудита заголовков.
        CASE WHEN COALESCE(text, '') ~* '\yчтиво\y'                         THEN 1 ELSE 0 END AS rubric_chtivo,
        CASE WHEN COALESCE(text, '') ~* 'истори[ия]\s+кумиров'               THEN 1 ELSE 0 END AS rubric_istorii_kumirov,
        CASE WHEN COALESCE(text, '') ~* 'истори[ия]\s+миллиардов'            THEN 1 ELSE 0 END AS rubric_istorii_milliardov,
        CASE WHEN COALESCE(text, '') ~* 'истори[яи]\s+бренда'                THEN 1 ELSE 0 END AS rubric_istoriya_brenda
    FROM source
),

-- Интервал до предыдущего поста (в днях) через LAG.
-- Первый пост даёт NULL — это нормально.
with_interval AS (
    SELECT
        post_id,
        post_date,
        EXTRACT(EPOCH FROM (
            post_date - LAG(post_date) OVER (ORDER BY post_date)
        )) / 86400.0 AS days_since_previous_post
    FROM source
),

-- Приведение типов и защита от NULL.
cleaned AS (
    SELECT
        s.post_id,
        s.post_date,
        COALESCE(s.text, '')                     AS text,
        COALESCE(s.likes, 0)                     AS likes,
        COALESCE(s.comments, 0)                  AS comments,
        COALESCE(s.reposts, 0)                   AS reposts,
        COALESCE(s.views, 0)                     AS views,
        COALESCE(s.is_deleted, FALSE)            AS is_deleted,
        COALESCE(r.rubric_uroki_istorii, 0)      AS rubric_uroki_istorii,
        COALESCE(r.rubric_seriynyi_nomer, 0)     AS rubric_seriynyi_nomer,
        COALESCE(r.rubric_vtoraya_mirovaya, 0)   AS rubric_vtoraya_mirovaya,
        COALESCE(r.rubric_prostoveshi, 0)        AS rubric_prostoveshi,
        COALESCE(r.rubric_doletskaya, 0)         AS rubric_doletskaya,
        COALESCE(r.rubric_lichnosti, 0)          AS rubric_lichnosti,
        COALESCE(r.rubric_chtivo, 0)             AS rubric_chtivo,
        COALESCE(r.rubric_istorii_kumirov, 0)    AS rubric_istorii_kumirov,
        COALESCE(r.rubric_istorii_milliardov, 0) AS rubric_istorii_milliardov,
        COALESCE(r.rubric_istoriya_brenda, 0)    AS rubric_istoriya_brenda,
        i.days_since_previous_post
    FROM source s
    LEFT JOIN rubrics        r USING (post_id)
    LEFT JOIN with_interval  i USING (post_id)
),

final AS (
    SELECT
        post_id,
        post_date,
        text,
        likes,
        comments,
        reposts,
        views,
        (likes + comments + reposts)          AS total_engagement,
        -- ER: NULLIF защищает от деления на ноль.
        ROUND(
            (likes + comments + reposts)::numeric / NULLIF(views, 0),
            4
        )                                      AS engagement_rate,
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
        EXTRACT(HOUR   FROM post_date)::int    AS hour,
        EXTRACT(ISODOW FROM post_date)::int    AS weekday,       -- 1=Пн..7=Вс
        CASE EXTRACT(ISODOW FROM post_date)::int
            WHEN 1 THEN 'Пн'
            WHEN 2 THEN 'Вт'
            WHEN 3 THEN 'Ср'
            WHEN 4 THEN 'Чт'
            WHEN 5 THEN 'Пт'
            WHEN 6 THEN 'Сб'
            WHEN 7 THEN 'Вс'
        END                                    AS weekday_ru
    FROM cleaned
    WHERE is_deleted = FALSE
)

SELECT * FROM final