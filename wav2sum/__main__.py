"""Private entry points used by the native app's runtime."""

import logging
import sys


def main() -> None:
    args = sys.argv[1:]
    if args == ["setup"]:
        from wav2sum.settings import main as setup

        setup()
        return
    if args not in (["serve"], ["serve", "--cloud"]):
        sys.exit("Invalid app runtime command")

    from wav2sum.config import STATE_DIR, exclusive, load_config

    cfg = load_config()
    cfg.cloud = len(args) == 2
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    with exclusive(STATE_DIR / "daemon.lock", wait=False) as locked:
        if not locked:
            sys.exit("wav2sum daemon is already running")
        from wav2sum.daemon import serve

        serve(cfg, False)


if __name__ == "__main__":
    main()
