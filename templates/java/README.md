# agentic-ops for a Maven project

Copy into the root of your Java repo:

```bash
cp -r templates/java/.github templates/java/policy.yml <your-repo>/
mkdir -p <your-repo>/semgrep && cp semgrep/rules.yml <your-repo>/semgrep/
```

Then in the Java repo's GitHub settings:

1. **Secrets:** `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY`).
2. **Variables:** `LLM_PROVIDER=openai` if using OpenAI.
3. **Settings → Actions → General:** allow GitHub Actions to create and approve pull requests.

Adjust `JAVA_VERSION` in the workflow to your project. If `mvn compile` needs private
repositories or services in CI, set CodeQL `build-mode: none` and drop the compile step.

What runs on each PR: CodeQL (java-kotlin) → Semgrep with the Java rules in `semgrep/rules.yml`
(SQL built by concatenation, command injection, unsafe deserialization, MD5/SHA-1, empty catch,
hard-coded credentials) → AI review with inline comments.

Not included yet: test generation (Python/pytest only) and Java linters (PMD/SpotBugs).
