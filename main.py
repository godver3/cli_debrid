import asyncio
import os
from questionary import Choice, select
from settings import SettingsEditor
from run_program import run_program_from_menu
from logging_config import get_logger, engage_custom_handler, disengage_custom_handler, remove_console_handler, add_console_handler
from utilities.debug_commands import debug_commands
from utilities.manual_scrape import run_manual_scrape

logger = get_logger()

def main_menu():
    os.system('clear')
    logger.info("Main menu started")

    while True:
        action = select(
            "Select an action:",
            choices=[
                Choice("Run Program", "run"),
                Choice("Edit Settings", "settings"),
                Choice("Manual Scrape", "scrape"),
                Choice("Debug Commands", "debug"),
                Choice("Exit", "exit")
            ]
        ).ask()

        os.system('clear')

        if action == "run":
            engage_custom_handler()
            remove_console_handler()
            asyncio.run(run_program_from_menu())
            disengage_custom_handler()
            add_console_handler()
        elif action == "settings":
            SettingsEditor()
        elif action == "scrape":
            asyncio.run(run_manual_scrape())
        elif action == "debug":
            debug_commands()
        elif action == "exit":
            logger.info("Exiting program.")
            break

        logger.info("Returned to main menu.")

if __name__ == "__main__":
    disengage_custom_handler()
    main_menu()
