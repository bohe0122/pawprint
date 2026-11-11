"""Fill in the README's sample-query comparison: for each sample query, the same model with no tools
and no system prompt, next to the agent's answer.

    gcloud auth application-default login
    uv run compare.py

Writes docs/sample_answers.md (full answers, with the agent's tool calls) and replaces the block
between <!-- COMPARISON:START --> and <!-- COMPARISON:END --> in README.md with a summary table.
"""

import asyncio
import json
import re
from pathlib import Path

import litellm

import app

HERE = Path(__file__).parent
SAMPLES = [
    "What factors are associated with longer shelter stays for dogs?",
    "How do senior cats fare compared with kittens and adult cats?",
    "How have adoption and euthanasia rates changed since 2014?",
]


def no_tools(question: str) -> str:
    """The same model, no tools, no system prompt: just the question."""
    response = litellm.completion(
        model=app.MODEL.model, messages=[{"role": "user", "content": question}],
        timeout=120, **({"vertex_location": "global"} if app.VERTEX else {}),
    )
    return response.choices[0].message.content.strip()


async def agent(question: str) -> tuple[str, list[dict]]:
    _, hooks = app.new_run(lambda event: None)
    result = await app.answer(f"compare-{abs(hash(question))}", question, hooks)
    return result.markdown, hooks.tool_calls


def first_paragraph(markdown: str, limit: int = 420) -> str:
    """The answer's opening paragraph, flattened onto one line for a table cell."""
    body = re.sub(r"^#+ .*$", "", markdown.split("## Sources")[0], flags=re.M).strip()
    para = re.split(r"\n\s*\n", body)[0].replace("\n", " ").replace("|", "/")
    return para if len(para) <= limit else para[:limit].rsplit(" ", 1)[0] + "..."


async def main():
    full, rows = ["# Sample answers: no tools vs. the agent\n"], []
    for i, question in enumerate(SAMPLES, 1):
        print(f"[{i}/{len(SAMPLES)}] {question}")
        baseline = await asyncio.to_thread(no_tools, question)
        report, calls = await agent(question)
        researchers = sorted({c["agent"] for c in calls if c["agent"].startswith("Researcher")})
        rounds = sum(1 for c in calls if c["name"] == "dispatch_researchers")
        full += [
            f"## {i}. {question}\n",
            "### Same model, no tools, no system prompt\n", baseline, "",
            f"### Agent ({len(researchers)} researchers over {rounds} round(s), {len(calls)} tool calls)\n", report, "",
            "<details><summary>Tool calls</summary>\n\n```json", json.dumps(calls, indent=1)[:20000], "```\n</details>\n",
        ]
        rows.append(f"| {i}. {question} | {first_paragraph(baseline)} | {first_paragraph(report)} |")

    docs = HERE / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "sample_answers.md").write_text("\n".join(full))

    table = "\n".join([
        "| Query | Same model, no tools, no system prompt | Pawprint agent |",
        "|---|---|---|",
        *rows,
        "",
        "Full answers, with every tool call, are in [docs/sample_answers.md](docs/sample_answers.md).",
    ])
    readme = (HERE / "README.md").read_text()
    readme = re.sub(r"<!-- COMPARISON:START -->.*?<!-- COMPARISON:END -->",
                    lambda m: f"<!-- COMPARISON:START -->\n{table}\n<!-- COMPARISON:END -->", readme, flags=re.S)
    (HERE / "README.md").write_text(readme)
    print("wrote docs/sample_answers.md and updated README.md")


if __name__ == "__main__":
    asyncio.run(main())
