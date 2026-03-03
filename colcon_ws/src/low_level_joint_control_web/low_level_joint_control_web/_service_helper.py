"""
_service_helper.py — compatibility shim.

The real implementation has moved to go2_common._service_subprocess.
This file is no longer invoked directly by this package.
"""
from go2_common._service_subprocess import main  # noqa: F401

if __name__ == '__main__':
    main()
