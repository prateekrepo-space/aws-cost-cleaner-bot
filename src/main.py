"""
Entrypoint for AWS ECS Fargate container execution.
Invokes the Cost-Cleaner handler and exits with appropriate process exit code.
"""

import sys
from lambda_function import lambda_handler

if __name__ == "__main__":
    result = lambda_handler()
    sys.exit(0 if result.get("statusCode") == 200 else 1)
