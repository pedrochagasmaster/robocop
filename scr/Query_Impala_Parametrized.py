# pylint: disable=line-too-long,trailing-whitespace,missing-final-newline,too-many-arguments,too-many-positional-arguments,f-string-without-interpolation,consider-using-with,unspecified-encoding,logging-not-lazy,consider-using-f-string,no-else-return
import logging
import argparse
import sys
import os

from _common import FATAL_ERRORS, classificar_erro_impala, cycle_through_pools, resolve_pools, run_impala_shell, send_email

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# =============================================================================
# Main Functions
# =============================================================================

def main():
    # Parse arguments
    parser = argparse.ArgumentParser(description='Execute a SQL query from a file, create a table, and send an email notification.')
    parser.add_argument('--sql-file', dest='sql_file', required=True,
                        help='Path to the .sql file containing the query to execute.')
    parser.add_argument('--table-name', dest='table_name', required=True,
                        help='Name of the table to be created (e.g., schema.tablename).')
    parser.add_argument('--to-email', dest='to_email', required=True,
                        help='Recipient email address(es). For multiple emails, separate with a semicolon (;).')
    parser.add_argument('--subject', dest='subject', required=True,
                        help='Subject line for the notification email.')
    parser.add_argument('--user', required=True, help='The remote user ID running the script.')
    parser.add_argument('--session-folder', dest='session_folder', required=True,
                        help='Full path to a unique session folder for all outputs.')

    args = parser.parse_args()

    # Assign variables from arguments
    tablecreated = args.table_name
    to_email = args.to_email
    subject = args.subject
    sql_query = load_query(args.sql_file)

    # Static variables that are not passed as arguments
    filas = resolve_pools(["adhoc_fast", "acs_small", "adhoc_small", "acs_large","adhoc"])

    print("--- Script Configuration ---")
    print(f"User: {args.user}")
    print(f"Table to be created: {tablecreated}")
    print(f"Recipient Emails: {to_email}")
    print(f"Email Subject: {subject}")
    print("--------------------------")

    ## The sql_query variable is now populated from the file content
    print("\nExecuting the following SQL query:")
    print("="*40)
    print(sql_query)
    print("="*40)

    try:
        os.makedirs(args.session_folder, exist_ok=True)
        print(f"Ensured session directory exists: {args.session_folder}")
    except OSError as e:
        print(f"Error creating session directory: {e}")
        sys.exit(1)

    retry_loop(sql_query, filas, to_email, subject, tablecreated, args.user)

def retry_loop(sql_query, filas, to_email, subject, tablecreated, user):
    messageBody = (
        f"User: {user}\n"
        f"Process: Table Creation\n"
        f"Table: {tablecreated}\n\n"
        "The script has started and will now attempt to execute the query."
    )
    subject_start = f"{subject} - PROCESSO INICIADO"
    send_email(messageBody, subject_start, to_email)

    def operation(fila):
        sql_pool = f"set request_pool={fila};"
        sql = sql_pool + " " + sql_query
        return run_on_impala(sql, subject, to_email, tablecreated, user, fila)

    def on_cycle_failure(retry_cnt):
        print("Nenhuma fila funcionou. Aguardando 30 segundos antes de tentar novamente...\n")
        messageBody = (
            f"User: {user}\n"
            f"Table: {tablecreated}\n"
            f"Attempt: {retry_cnt}\n\n"
            "All Impala queues (adhoc_fast, adhoc_small, adhoc) are currently busy. \n"
            "The script will wait for 30 seconds and then retry the execution cycle. "
            "You will be notified upon success or fatal error."
        )
        subject_filaCheia = f"{subject} - (Attempt {retry_cnt}) All Queues Full"
        send_email(messageBody, subject_filaCheia, to_email)

    return cycle_through_pools(filas, operation, on_cycle_failure)

# =============================================================================
# Helper Functions
# =============================================================================

def load_query(path):
    try:
        with open(path, 'r') as f:
            sql_query = f.read()
        print(f"Successfully loaded SQL from '{path}'")
    except FileNotFoundError:
        print(f"Error: The file '{path}' was not found.")
        sys.exit(1)
    return sql_query

def run_on_impala(query: str, subject, to_email, tablecreated="", user="", queue=""):
    returncode, stdout, stderr = run_impala_shell(
        ['impala-shell', '-k', '-i', 'dw.prod.impala.mastercard.int:21000', '--ssl', '--delimited', '--print_header',
         '--output_delimiter=|', '-q', query], pool=queue)
    logging.info("Executing query: %s" % query)

    if returncode == 0:
        print("########## query executed successfully ##########")
        messageBody = (
            f"User: {user}\n"
            f"Process: Table Creation\n"
            f"Status: SUCCESS\n"
            f"Table Created: {tablecreated}\n"
            f"Succeeded on Queue: {queue}\n\n"
            "The SQL query was executed successfully."
        )
        subject_success = f"{subject} - PROCESSO FINALIZADO"
        send_email(messageBody, subject_success, to_email)
        logging.debug(stdout.decode())
        return True
    else:
        print("********ERROR********")
        stderr_decoded = stderr.decode()
        erro_classificado = classificar_erro_impala(stderr_decoded)
        print(f"Erro mapeado: {erro_classificado['categoria']}")
        
        if erro_classificado['categoria'] in FATAL_ERRORS:
            messageBody = (
                f"User: {user}\n"
                f"Process: Table Creation\n"
                f"Status: FATAL ERROR\n"
                f"Table: {tablecreated}\n"
                f"Failed on Queue: {queue}\n"
                f"Error Type: {erro_classificado['categoria']}\n\n"
                f"A fatal error occurred, and the process will not be retried. Please review the details below.\n\n"
                f"------------------- ERROR TRACE -------------------\n"
                f"{erro_classificado['detalhes']}\n"
                f"---------------------------------------------------\n"
            )
            subject_error = f"{subject} - ERRO ({erro_classificado['categoria']})"
            send_email(messageBody, subject_error, to_email)
            logging.debug(stdout.decode())
            sys.exit(1)
        else:
            ## Retriable Error Message
            messageBody = (
                f"User: {user}\n"
                f"Process: Table Creation\n"
                f"Status: RETRIABLE ERROR\n"
                f"Table: {tablecreated}\n"
                f"Queue Attempted: {queue}\n"
                f"Error Type: {erro_classificado['categoria']}\n\n"
                f"A retriable error occurred. The script will attempt to run the query again using the next available queue.\n\n"
                f"------------------- ERROR TRACE -------------------\n"
                f"{erro_classificado['detalhes']}\n"
                f"---------------------------------------------------\n"
            )
            subject_retry = f"{subject} - RETRIABLE ERROR ({erro_classificado['categoria']})"
            send_email(messageBody, subject_retry, to_email)
        return False

if __name__ == '__main__':
    main()
