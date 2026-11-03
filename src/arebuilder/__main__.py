import os
import sys

if __name__ == "__main__":
    # Keep the host parent free of native dependencies. Containerized AREDev
    # retains its existing host-launcher protocol without running host pip.
    if (
        sys.argv[1:2] == ["aredev"]
        and not os.environ.get("AREDEV_NATIVE_UPDATE_REQUEST")
        and os.environ.get("AREDEV_IN_CONTAINER") != "1"
    ):
        from arebuilder._native_update import main

        raise SystemExit(main(sys.argv[1:]))
    from arebuilder.app.cli import main

    raise SystemExit(main())
