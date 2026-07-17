# pylint: disable=line-too-long,trailing-whitespace,missing-final-newline,logging-fstring-interpolation,too-many-locals,f-string-without-interpolation,unspecified-encoding
import argparse
import calendar
import logging
import sys
from datetime import datetime, timedelta

try:
    from Query_Impala_Parametrized import run_on_impala
    from _common import cycle_through_pools, resolve_pools, send_email, validate_identifier
except ImportError:
    logging.error("Fatal Error: Could not import functions from Query_Impala_Parametrized.py.")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

DATE_INICIO_TOKEN = "{date_inicio}"
DATE_FIM_TOKEN = "{date_fim}"


def monthly_range(start_date, end_date):
    """Generator for iterating through the first day of each month in a date range."""
    current_date = start_date.replace(day=1)
    while current_date <= end_date:
        yield current_date
        current_date = (current_date + timedelta(days=32)).replace(day=1)


def render_monthly_sql(sql_template: str, date_inicio: str, date_fim: str) -> str:
    if DATE_INICIO_TOKEN not in sql_template or DATE_FIM_TOKEN not in sql_template:
        raise ValueError(
            "Monthly SQL template must include {date_inicio} and {date_fim} tokens."
        )
    return sql_template.replace(DATE_INICIO_TOKEN, date_inicio).replace(
        DATE_FIM_TOKEN, date_fim
    )


def _scan_quoted_character(sql: str, index: int, state: str):
    char = sql[index]
    next_char = sql[index + 1] if index + 1 < len(sql) else ""
    quote = "'" if state == "single_quote" else '"'
    if char == "\\" and next_char:
        return index + 1, state, index + 1
    if char != quote:
        return index, state, index
    if next_char == quote:
        return index + 1, state, index + 1
    return index, "sql", index


def _scan_sql_character(sql: str, index: int):
    char = sql[index]
    next_char = sql[index + 1] if index + 1 < len(sql) else ""
    if char == "-" and next_char == "-":
        return index + 1, "line_comment", None
    if char == "/" and next_char == "*":
        return index + 1, "block_comment", None
    if char == "'":
        return index, "single_quote", index
    if char == '"':
        return index, "double_quote", index
    return index, "sql", index if not char.isspace() else None


def _strip_terminal_semicolon(sql: str) -> str:
    """Remove the final SQL delimiter while preserving comments and quoted text."""
    state = "sql"
    last_significant_index = None
    index = 0

    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""

        if state == "line_comment":
            if char == "\n":
                state = "sql"
        elif state == "block_comment":
            if char == "*" and next_char == "/":
                index += 1
                state = "sql"
        elif state in {"single_quote", "double_quote"}:
            index, state, last_significant_index = _scan_quoted_character(sql, index, state)
        else:
            index, state, significant_index = _scan_sql_character(sql, index)
            if significant_index is not None:
                last_significant_index = significant_index

        index += 1

    if last_significant_index is not None and sql[last_significant_index] == ";":
        return sql[:last_significant_index] + sql[last_significant_index + 1:]
    return sql


def execute_step_with_retry(query: str, operation_desc: str, args):
    """
    Manages the retry logic for a single logical monthly job.

    The entire temp-create -> final-join -> cleanup script is sent to one
    impala-shell process.  That keeps all statements on the same coordinator,
    avoiding intermittent TABLE_NOT_FOUND failures when a load-balanced join
    lands on a different local-catalog coordinator than the temp table creates.
    """
    logging.info(f"--- Starting Step: {operation_desc} ---")
    pools = resolve_pools(["adhoc_fast", "adhoc_small", "acs_small", "acs_large", "adhoc"])

    def operation(pool):
        full_query = f"set request_pool={pool}; {query}"
        step_subject = f"{args.subject} - Step: {operation_desc}"
        finished = run_on_impala(
            query=full_query,
            subject=step_subject,
            to_email=args.to_email,
            tablecreated=operation_desc,
            user=args.user,
            queue=pool
        )
        if finished:
            logging.info(f"Monthly job '{operation_desc}' finished on queue {pool}. Check email notifications for final status.")
        return finished

    def on_cycle_failure(_retry_cnt):
        logging.warning(f"All queues failed for monthly job '{operation_desc}' with retryable errors. Waiting 30 seconds.")

    try:
        cycle_through_pools(pools, operation, on_cycle_failure, max_cycles=10)
    except TimeoutError as exc:
        raise TimeoutError(f"Monthly job '{operation_desc}' failed after 10 retry cycles. Halting job.") from exc


def build_monthly_job_query(args, sql_template: str) -> tuple[str, list[str], str]:
    """Build one Impala script for all monthly table work.

    Returning one script lets the caller execute temp table creation, final
    table creation, and temp cleanup in a single impala-shell session pinned to
    one coordinator.
    """
    start_date = datetime.strptime(args.start_date, "%m/%d/%Y")
    end_date = datetime.strptime(args.end_date, "%m/%d/%Y")

    # HDFS storage prefix is the schema's leading segment (e.g. aa_enc -> aa,
    # coe_enc -> coe), matching the table_wrapper used by Query_Impala.
    schema_prefix = args.schema.split("_", 1)[0]
    planned_temp_tables = [
        f"{args.schema}.{args.table_name}_temp_{date.strftime('%Y%m')}"
        for date in monthly_range(start_date, end_date)
    ]
    final_table_name = f"{args.schema}.{args.table_name}_fulljoin"

    statements = []
    for date in monthly_range(start_date, end_date):
        month_end_day = calendar.monthrange(date.year, date.month)[1]
        month_end_date = date.replace(day=month_end_day)
        date_inicio_str, date_fim_str = str(date.date()), str(month_end_date.date())
        year_month = date.strftime('%Y%m')
        temp_table_name = f"{args.schema}.{args.table_name}_temp_{year_month}"
        monthly_sql = _strip_terminal_semicolon(
            render_monthly_sql(sql_template, date_inicio_str, date_fim_str)
        )
        statements.append(f"""
            DROP TABLE IF EXISTS {temp_table_name};
            CREATE TABLE {temp_table_name}
            STORED AS parquet LOCATION '/das/{schema_prefix}/enc/{args.user}/{args.table_name}_temp_{year_month}'
            AS
            {monthly_sql}
            ;
        """)

    union_query_parts = [f"SELECT * FROM {table}" for table in planned_temp_tables]
    union_query = "\nUNION ALL\n".join(union_query_parts)
    statements.append(f"""
        DROP TABLE IF EXISTS {final_table_name};
        CREATE TABLE {final_table_name}
        STORED AS parquet LOCATION '/das/{schema_prefix}/enc/{args.user}/{args.table_name}_fulljoin' AS
        {union_query};
    """)

    for table in planned_temp_tables:
        statements.append(f"DROP TABLE IF EXISTS {table};")

    return "\n".join(statements), planned_temp_tables, final_table_name


def process_monthly_job(args):
    """
    Orchestrates the entire monthly partitioned job from planning to cleanup.
    """
    with open(args.sql_file, 'r') as f:
        sql_template = f.read()

    monthly_query, planned_temp_tables, final_table_name = build_monthly_job_query(args, sql_template)

    plan_message = ("Monthly partitioned job has started.\n\nExecution Plan:\n-----------------\n"
                    "1. The following temporary tables will be created:\n")
    for table in planned_temp_tables:
        plan_message += f"   - {table}\n"
    plan_message += (f"\n2. The temporary tables will be joined into the final table:\n   - {final_table_name}\n\n"
                     f"3. All temporary tables will be deleted upon completion.\n\n"
                     f"All SQL statements will run in one Impala shell session so the job remains on one coordinator.\n\n"
                     f"You will receive further notifications as the job progresses.")
    send_email(plan_message, f"{args.subject} - Job Started (Execution Plan)", args.to_email)

    execute_step_with_retry(monthly_query, f"Monthly partitioned job {final_table_name}", args)

    send_email("The entire monthly partitioned job has completed.", f"{args.subject} - Job Finished", args.to_email)


# --- Main Entry Point ---
def main():
    """
    Parses arguments and calls the main processing function.
    """
    parser = argparse.ArgumentParser(description='Run a monthly-partitioned Impala query job.')
    parser.add_argument('--sql-file', required=True, help='Path to the .sql template file.')
    parser.add_argument('--schema', required=True, help='Target schema.')
    parser.add_argument('--table-name', required=True, help='Base name for the final table.')
    parser.add_argument('--start-date', required=True, help='Start date (MM/DD/YYYY).')
    parser.add_argument('--end-date', required=True, help='End date (MM/DD/YYYY).')
    parser.add_argument('--user', required=True, help='Remote user ID (eid).')
    parser.add_argument('--to-email', required=True, help='Recipient email address.')
    parser.add_argument('--subject', required=True, help='Base subject for emails.')
    args = parser.parse_args()

    for value, flag in (
        (args.schema, "--schema"),
        (args.table_name, "--table-name"),
        (args.user, "--user"),
    ):
        if not validate_identifier(value):
            parser.error(f"{flag} must be a plain Impala identifier")

    try:
        process_monthly_job(args)
    except Exception as exc:
        logging.error(f"A critical error occurred in the monthly job orchestrator: {exc}")
        send_email(f"The monthly partitioned job failed with a critical error in the main script:\n\n{exc}", f"{args.subject} - JOB FAILED", args.to_email)
        sys.exit(1)

if __name__ == '__main__':
    main()
