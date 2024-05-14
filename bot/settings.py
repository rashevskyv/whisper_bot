import logging

# Налаштування логування
logger = logging.getLogger(__name__)

def error(update, context):
    logger.warning(f'Update {update} caused error {context.error}')
