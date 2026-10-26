"""Set a new password for a tvcal account, from your own machine.

For when someone forgets theirs: accounts have no email address, so there's
no self-service reset. This also signs the account out on every device.
Share the new password with them and have them change it in the app
("Change password", at the bottom of the home page).

Needs the app's requirements installed (pip install -r requirements.txt)
and the Function App's storage connection string, passed for just this one
command so it doesn't linger in your shell:

    AzureWebJobsStorage="$(az functionapp config appsettings list \\
        --name <function-app-name> --resource-group <resource-group> \\
        --query "[?name=='AzureWebJobsStorage'].value" -o tsv)" \\
        python scripts/reset_password.py <username>
"""
import getpass
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import auth  # noqa: E402
import storage  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: python scripts/reset_password.py <username>", file=sys.stderr)
        return 2
    if not os.environ.get("AzureWebJobsStorage"):
        print(
            "Set AzureWebJobsStorage to the Function App's storage connection string first "
            "(see the top of this script).",
            file=sys.stderr,
        )
        return 2

    try:
        username = auth.validate_username(argv[1])
        account = storage.get_account(username)
    except auth.InvalidUsername as exc:
        print(exc, file=sys.stderr)
        return 1
    except storage.UnknownAccount:
        print(f"No account named '{argv[1]}'.", file=sys.stderr)
        return 1

    password = getpass.getpass(f"New password for {username}: ")
    if getpass.getpass("Same password again: ") != password:
        print("The passwords don't match; nothing was changed.", file=sys.stderr)
        return 1

    try:
        auth.change_password(username, password, account["etag"])
    except auth.InvalidPassword as exc:
        print(f"{exc} Nothing was changed.", file=sys.stderr)
        return 1
    except storage.AccountChanged:
        print(
            f"'{username}' changed their password just now, so nothing was changed. "
            "Run this again if they still need a reset.",
            file=sys.stderr,
        )
        return 1

    print(f"Password for '{username}' changed, and they've been signed out everywhere.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
