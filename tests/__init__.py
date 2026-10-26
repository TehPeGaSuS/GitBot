"""GitBot tests: python -m unittest discover"""

import logging

# Keep the expected warnings (wrong passwords, refused webhooks...) out of
# the test output.
logging.disable(logging.CRITICAL)
