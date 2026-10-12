"""Current parser implementation and dependency identity."""

import platform
from importlib.metadata import version
from pathlib import Path

from .core import digest


def parser_version():
    """Bind projections to implementation bytes and actual parsing dependencies."""
    root = Path(__file__).parent
    return digest(
        {
            "contract": "document-parser.v1",
            "python": platform.python_version(),
            "modules": {
                name: digest((root / name).read_bytes())
                for name in (
                    "document_parser.py",
                    "document_office.py",
                    "attachments.py",
                    "html_safety.py",
                    "core.py",
                    "content.py",
                    "parser_identity.py",
                )
            },
            "dependencies": {
                name: version(name)
                for name in (
                    "openpyxl",
                    "et-xmlfile",
                    "python-docx",
                    "xlrd",
                    "lxml",
                    "pypdf",
                    "pillow",
                    "parsel",
                    "scrapy",
                    "w3lib",
                )
            },
        }
    )
