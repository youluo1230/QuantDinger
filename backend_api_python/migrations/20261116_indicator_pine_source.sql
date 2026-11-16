-- Indicators are Pine Script now, and the chart runs them in the browser.
--
-- The Indicator IDE used to hold Python: the editor text was Python, the
-- backend sandbox evaluated it, and ``chart-preview`` shipped the resulting
-- arrays to the frontend for replay. Charting a Pine script therefore meant
-- transpiling it to Python, executing it per bar in a subprocess, and drawing
-- the precomputed numbers -- seconds of latency for a study the browser can
-- evaluate in milliseconds.
--
-- ``code`` is the text the user edits, so it now holds the Pine source. The
-- transpiled Python that used to live there is still produced on demand for
-- strategies; it is simply no longer what the IDE stores.
--
-- Identifying the rows to rewrite: ``source_language = 'pine'`` does NOT mean
-- the column holds Pine. The conversion pipeline set that marker and then wrote
-- the *transpiled Python* into ``code``, so today all 129 such rows start with
-- the transpiler's generated docstring. That leading ``"""`` is the reliable
-- signature of machine-generated output, and a row whose ``code`` no longer
-- starts with it is an author's own edit, which must be preserved.
--
-- The script a converted indicator came from is named in
-- ``qd_tv_indicators.diagnostics``. Rows the join cannot reach are left alone.

UPDATE qd_indicator_codes AS c
   SET code = s.source_code,
       source_language = 'pine',
       updated_at = NOW()
  FROM qd_tv_scripts AS s
  LEFT JOIN qd_tv_indicators AS i
         ON i.script_id_part = s.script_id_part
        AND i.status = 'published'
 WHERE s.source_code IS NOT NULL
   AND s.source_code <> ''
   AND ltrim(c.code) LIKE '"""%'
   -- Two routes to the same script. The diagnostics route is the conversion
   -- pipeline's own record of which row it published. Two rows lost that record
   -- (one was blanked by a later sandbox failure, one predates the field), and
   -- for those the pipeline's '[TV] ' name prefix is still unique because
   -- neither script carries the author suffix the prefix normally absorbs.
   -- ``jsonb_exists`` rather than the ``?`` operator: the migration runner
   -- rewrites every ``?`` to a ``%s`` placeholder for legacy callers.
   AND (
        (jsonb_exists(i.diagnostics, 'indicator_id')
         AND (i.diagnostics ->> 'indicator_id') = c.id::text)
        OR c.name = '[TV] ' || s.name
   );
