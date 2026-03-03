"""
_service_helper.py — compatibility shim.

The real implementation has moved to common._service_subprocess.
This file is no longer invoked directly by this package.
"""
from common._service_subprocess import main  # noqa: F401

if __name__ == '__main__':
    main()
