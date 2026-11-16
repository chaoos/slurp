"""Write the logo files of docs/images/ (logo.svg, logo-plain.svg, logo-wordmark.svg, logo-256.png, logo.ico) from
slurp's pixel map (src/slurp/ui/logo.py). Run it after changing the logo: python tools/make_logo_files.py"""
import os

from slurp.ui import logo

if __name__ == "__main__":
  folder = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "images")
  print(f"written to {folder}:", ", ".join(logo.write_docs(folder)))
