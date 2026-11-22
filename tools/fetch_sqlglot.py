"""Download sqlglot at the pinned commit from its own repository into /app/repo (build time only)."""
import io
import os
import shutil
import sys
import tarfile
import time
import urllib.request

SHA = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
dest = sys.argv[1] if len(sys.argv) > 1 else "/app/repo"
# (two addresses of the same archive, and pauses between tries: many images are built at once)
urls = ["https://github.com/tobymao/sqlglot/archive/%s.tar.gz" % SHA, "https://codeload.github.com/tobymao/sqlglot/tar.gz/%s" % SHA]
for attempt in range(10):
    try:
        data = urllib.request.urlopen(urls[attempt % 2], timeout=60).read()
        tarfile.open(fileobj=io.BytesIO(data)).getmembers()
        break
    except Exception as e:  # noqa: BLE001
        print("download failed (%r), retrying" % (e,), file=sys.stderr)
        time.sleep(min(30, 2 + 3 * attempt))
else:
    raise SystemExit("could not download sqlglot")
tmp = dest + "_src"
tarfile.open(fileobj=io.BytesIO(data)).extractall(tmp, filter="data")
os.rename(os.path.join(tmp, "sqlglot-" + SHA), dest)
shutil.rmtree(tmp)
print("sqlglot", SHA, "->", dest)
