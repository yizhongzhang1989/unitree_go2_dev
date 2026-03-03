"""
_msc_helper.py — compatibility shim.

The real implementation has moved to common._msc_helper.
This file is no longer invoked directly by this package.
"""
from common._msc_helper import main  # noqa: F401

if __name__ == '__main__':
    main()
