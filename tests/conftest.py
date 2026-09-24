import pytest

from agentic_ops.config import Settings

SAMPLE_DIFF = """diff --git a/app/users.py b/app/users.py
index 1111111..2222222 100644
--- a/app/users.py
+++ b/app/users.py
@@ -1,3 +1,7 @@
 import sqlite3
-
+import subprocess
+
+def run(cmd):
+    return subprocess.run(cmd, shell=True)
 def get_conn():
diff --git a/docs/readme.md b/docs/readme.md
index 1..2 100644
--- a/docs/readme.md
+++ b/docs/readme.md
@@ -1 +1,2 @@
 # Docs
+More docs
diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null
@@ -1 +0,0 @@
-x = 1
"""


@pytest.fixture
def sample_diff() -> str:
    return SAMPLE_DIFF


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(semgrep_config="semgrep/rules.yml", repo_dir=str(tmp_path), api_token="t0k")
