from pathlib import Path
import argparse
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from jty_materials.common import Config
from jty_materials.manager import serve

if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--config", required=True)
    serve(Config(parser.parse_args().config))
