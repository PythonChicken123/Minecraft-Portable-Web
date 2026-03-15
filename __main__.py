import sys
from portablemc.cli import main

if __name__ == "__main__":
    sys.argv = ["portablemc"] + sys.argv[1:]
    sys.exit(main())
