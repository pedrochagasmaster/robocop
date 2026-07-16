# pylint: disable=line-too-long,trailing-whitespace,missing-final-newline,no-else-return,logging-fstring-interpolation,consider-using-with,unspecified-encoding
import logging
import argparse
import sys
import os
import uuid

from _common import (
    FATAL_ERRORS,
    classificar_erro_impala,
    cycle_through_pools,
    resolve_pools,
    run_impala_shell,
    send_email,
    validate_full_table,
)

# Set up basic logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# =============================================================================
# Helper Functions
# =============================================================================

def _remove_temp_output(temp_output_file: str):
    try:
        os.remove(temp_output_file)
    except FileNotFoundError:
        pass


def _send_export_email(message_body: str, subject: str, to_email: str) -> None:
    if to_email:
        send_email(message_body, subject, to_email)


def run_export_on_impala(
    query: str,
    output_file: str,
    *,
    to_email: str = "",
    subject: str = "Dispatch CSV Export",
    queue: str = "",
):
    """
    Executes an Impala query to export data to a CSV file.
    """
    output_dir = os.path.dirname(output_file) or "."
    output_name = os.path.basename(output_file)
    temp_output_file = os.path.join(
        output_dir,
        f".{output_name}.{uuid.uuid4().hex}.tmp",
    )
    command = [
        'impala-shell', '-k', '-i', 'dw.prod.impala.mastercard.int:21000', '--ssl',
        '--delimited', '--print_header', '--output_delimiter=,',
        '-q', query, '-o', temp_output_file
    ]

    try:
        returncode, stdout, stderr = run_impala_shell(command, pool=queue)
    except Exception:
        _remove_temp_output(temp_output_file)
        raise

    if returncode == 0:
        try:
            os.replace(temp_output_file, output_file)
        except Exception:
            _remove_temp_output(temp_output_file)
            raise
        logging.info(f"SUCCESS: Successfully exported data to {output_file}")
        _send_export_email(
            (
                "Process: CSV Export\n"
                "Status: SUCCESS\n"
                f"Output File: {output_file}\n"
                f"Succeeded on Queue: {queue}\n\n"
                "The CSV export completed successfully."
            ),
            f"{subject} - PROCESSO FINALIZADO",
            to_email,
        )
        logging.debug(stdout.decode()) 
        return True
    else:
        _remove_temp_output(temp_output_file)
        logging.error(f"ERROR: Impala command failed for output file {output_file}.")
        
        stderr_decoded = stderr.decode()
        erro_classificado = classificar_erro_impala(stderr_decoded)
        logging.warning(f"Mapped Error: {erro_classificado['categoria']}")
        
        if erro_classificado['categoria'] in FATAL_ERRORS:
            logging.error(f"FATAL ERROR ({erro_classificado['categoria']}): Stopping retries.")
            logging.error(f"Error Details:\n{erro_classificado['detalhes']}")
            _send_export_email(
                (
                    "Process: CSV Export\n"
                    "Status: FATAL ERROR\n"
                    f"Output File: {output_file}\n"
                    f"Failed on Queue: {queue}\n"
                    f"Error Type: {erro_classificado['categoria']}\n\n"
                    "A fatal error occurred, and the export will not be retried. "
                    "Please review the details below.\n\n"
                    "------------------- ERROR TRACE -------------------\n"
                    f"{erro_classificado['detalhes']}\n"
                    "---------------------------------------------------\n"
                ),
                f"{subject} - ERRO ({erro_classificado['categoria']})",
                to_email,
            )
            sys.exit(1)
            
        logging.warning(f"Transient error detected. Details: {stderr_decoded}")
        _send_export_email(
            (
                "Process: CSV Export\n"
                "Status: RETRIABLE ERROR\n"
                f"Output File: {output_file}\n"
                f"Queue Attempted: {queue}\n"
                f"Error Type: {erro_classificado['categoria']}\n\n"
                "A retriable error occurred. The script will attempt to export again "
                "using the next available queue.\n\n"
                "------------------- ERROR TRACE -------------------\n"
                f"{erro_classificado['detalhes']}\n"
                "---------------------------------------------------\n"
            ),
            f"{subject} - RETRIABLE ERROR ({erro_classificado['categoria']})",
            to_email,
        )
        return False


def retry_loop(
    query_to_run: str,
    output_file: str,
    queues: list,
    *,
    to_email: str = "",
    subject: str = "Dispatch CSV Export",
):
    """
    Manages the retry logic for exporting data.
    """
    logging.info(f"--- Starting export process for {output_file} ---")
    _send_export_email(
        (
            "Process: CSV Export\n"
            f"Output File: {output_file}\n\n"
            "The script has started and will now attempt to export the CSV."
        ),
        f"{subject} - PROCESSO INICIADO",
        to_email,
    )
    
    def operation(fila):
        query_string = f"set request_pool={fila}; set mem_limit=1000g; {query_to_run}"
        logging.info(f"Attempting export with queue: {fila}")
        return run_export_on_impala(
            query_string,
            output_file,
            to_email=to_email,
            subject=subject,
            queue=fila,
        )

    def on_cycle_failure(retry_cnt):
        log_msg = (f"All queues failed for export job. "
                   f"Waiting 30 seconds before retrying (Attempt {retry_cnt}).")
        logging.warning(log_msg)
        _send_export_email(
            (
                "Process: CSV Export\n"
                f"Output File: {output_file}\n"
                f"Attempt: {retry_cnt}\n\n"
                "All Impala queues are currently busy. The script will wait for "
                "30 seconds and then retry the export cycle."
            ),
            f"{subject} - (Attempt {retry_cnt}) All Queues Full",
            to_email,
        )

    cycle_through_pools(queues, operation, on_cycle_failure)
    logging.info(f"--- Finished export process for {output_file} ---")


# =============================================================================
# Main Function
# =============================================================================

def main():
    """
    Main function to parse arguments and orchestrate the export process.
    """
    parser = argparse.ArgumentParser(
        description='Export Impala data to a raw CSV file with a retry mechanism.'
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        '--table-name',
        dest='table_name',
        help='The full name of the Impala table to export (e.g., schema.tablename).'
    ) 
    group.add_argument(
        '--query-file',
        dest='query_file',
        help='Path to a file containing the SQL SELECT query to execute and export.'
    )
    parser.add_argument(
        '--output-file',
        dest='output_file',
        required=True,
        help='The full path for the output CSV file (e.g., /path/to/output.csv). Compression must be handled by the caller.'
    )
    parser.add_argument(
        '--to-email',
        dest='to_email',
        default='',
        help='Optional recipient email address(es). For multiple emails, separate with a semicolon (;).'
    )
    parser.add_argument(
        '--subject',
        dest='subject',
        default='Dispatch CSV Export',
        help='Optional subject line for notification emails.'
    )
    args = parser.parse_args() 

    # Determine the query to run based on arguments
    query_to_run = ""
    if args.table_name:
        if not validate_full_table(args.table_name):
            parser.error(
                "--table-name must be schema.table using plain Impala identifiers"
            )
        logging.info(f"Mode: Exporting table '{args.table_name}'")
        query_to_run = f"select * from {args.table_name};"
    elif args.query_file:
        logging.info(f"Mode: Executing query from file '{args.query_file}'")
        try:
            with open(args.query_file, 'r') as f:
                query_to_run = f.read()
            if not query_to_run.strip():
                logging.error(f"ERROR: The provided query file '{args.query_file}' is empty.")
                sys.exit(1)
        except FileNotFoundError:
            logging.error(f"ERROR: The query file '{args.query_file}' was not found.")
            sys.exit(1)

    filas = resolve_pools(["adhoc_fast", "adhoc_small", "adhoc"]) 

    logging.info("--- Script Configuration ---")
    logging.info(f"Output CSV file: {args.output_file}")
    logging.info(f"Recipient Emails: {args.to_email or '--'}")
    logging.info(f"Email Subject: {args.subject}")
    logging.info("--------------------------")

    retry_loop(
        query_to_run,
        args.output_file,
        filas,
        to_email=args.to_email,
        subject=args.subject,
    )

    logging.info(f"Export job for {args.output_file} is complete.")


if __name__ == '__main__':
    main()
