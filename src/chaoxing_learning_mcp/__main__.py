import sys

if '--bridge' in sys.argv[1:]:
    from .persistent_bridge import main
elif '--proxy' in sys.argv[1:]:
    from .stdio_proxy import main
else:
    from .studyflow import main

main()
