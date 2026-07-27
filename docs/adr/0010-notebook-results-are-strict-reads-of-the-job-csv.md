# Notebook Results are strict reads of a Job's CSV, kept in the Notebook workspace

A notebook Result is read back from the CSV the Job's Destination already
wrote: `job.rows()` parses it with the standard library, `job.to_df()` hands it
to pandas. Dispatch adds no new export path, and no query result ever travels
through the CLI's stdout.

Results for notebook Jobs land in the Notebook workspace
(`<data_root>/.dispatch/notebook/<query>/`), one directory per query, rather
than in the Analyst's working directory.

## Considered options

- **Write notebook Results into the Analyst's working directory**, as
  ADR-0003 requires for CSV Jobs. Rejected for Inline SQL. ADR-0003 exists so a
  CSV the Analyst asked for lands where they are standing instead of inside
  `~/.dispatch/`; a Result fetched into a DataFrame is a transport buffer with a
  generated name, and dropping `nb_a1b2c3.csv` beside someone's queries is
  litter, not delivery. This refines ADR-0003 rather than reversing it:
  Destination CSVs that an Analyst names still go to the working directory, and
  `job.to_csv(path)` copies a Result anywhere on request.
- **One shared workspace directory instead of one directory per query.**
  Rejected: `sql.safe_csv_path` requires the CSV to be a direct child of the
  launch directory, so two exports of the same table would collide on one
  filename and an older `Job.to_df()` would silently return newer data. A
  directory per query also makes cleanup a single `rmtree`.
- **Return rows over the CLI (a new `dispatch job results --json`).**
  Rejected: it would mean a second export path, buffering a whole result set
  through a pipe and JSON, when a file already exists on a filesystem both
  processes share.
- **Depend on pandas.** Rejected. Runtime dependencies are `textual` and
  `sqlglot`; Analysts never run pip, and the shared runtime is Release-Operator
  controlled (ADR-0007). pandas is an optional import: `to_df()` explains what
  to ask for when it is missing, and `rows()`/`columns` always work.

## Consequences

- `scr/download_to_csv.py` exports with impala-shell's
  `--delimited --print_header --output_delimiter=,`, which **does not quote or
  escape fields**. A string value containing a comma or a newline produces an
  ambiguous CSV. Dispatch therefore reads with quoting disabled and refuses to
  guess: `rows()` raises `ResultParseError` naming the line whose field count
  disagrees with the header, and `to_df()` passes pandas' own strict mode. We
  never skip bad lines — silently dropping rows in an analytics tool is worse
  than failing. A row split by an embedded newline can still pad silently under
  pandas, so `rows()` is the integrity check.
- CSV carries no types, so a Result's dtypes are whatever pandas infers.
  `to_df(**read_csv_kwargs)` exists so callers can pass `dtype=`/`parse_dates=`.
- Results are durable: they are not deleted after being read, because a Job's
  artifacts outliving the kernel is what makes a detached runner useful.
  `Dispatch.cleanup()` reclaims them, defaulting to the same seven days the
  dashboard uses for active Jobs.
- `to_df()` works on any Job with a CSV Destination, including Jobs launched
  from the TUI or the CLI in the Analyst's own directory. Nothing about reading
  a Result is notebook-specific.
