import logging
from logging.handlers import RotatingFileHandler

class QueueHandler(logging.Handler):
    def __init__(self, log_queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        try:
            msg = self.format(record)
            self.log_queue.append(msg + "\n")
        except Exception:
            self.handleError(record)

def setup_logging(log_queue=None):
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    # clear existing handlers
    logger.handlers = []
    
    # Rotating file handler
    file_handler = RotatingFileHandler("harness.log", maxBytes=5*1024*1024, backupCount=2)
    file_fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(file_fmt)
    logger.addHandler(file_handler)
    
    if log_queue is not None:
        queue_handler = QueueHandler(log_queue)
        queue_fmt = logging.Formatter('%(message)s')
        queue_handler.setFormatter(queue_fmt)
        logger.addHandler(queue_handler)
