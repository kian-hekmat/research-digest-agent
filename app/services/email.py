"""Email delivery via SMTP, plus rendering a batch of digests into an email.

`EmailSender` wraps smtplib so it can be injected as a Temporal Activity and
faked in tests - constructing it never opens a connection, only `.send()`
does, and the SMTP client itself is swappable via `smtp_client_factory` (same
injection pattern as `Summarizer(http_client=...)`). `build_topic_section` and
`render_digest_email` are pure functions, independently testable without a
DB, SMTP, or Temporal.

Papers are dated and grouped by their own arXiv submission date, never by
when the digest run that found them happened: an email covers several daily
runs, most of which find nothing, and one run can find papers from many
different days. Grouping by run date is what produced an email with the same
date header four times over (four runs that day, three of them empty) and 25
papers all filed under the day they were fetched rather than submitted.
"""
from __future__ import annotations

import dataclasses
import re
import smtplib
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from itertools import groupby

from app.config import get_settings
from app.services.ranking import score_papers
from app.temporal.types import DigestEmailContent


@dataclass
class PaperForEmail:
    title: str
    summary: str | None
    arxiv_id: str
    published_at: datetime | None = None
    # Ranking signals - see app.services.ranking. None = unknown.
    relevance: int | None = None
    max_author_h_index: int | None = None
    venue_score: float = 0.0
    prior_similarity: float | None = None
    # Retrieval (app.services.retrieval): a sentence relating this paper to
    # earlier ones, and the arXiv id of an earlier paper it near-duplicates.
    related_note: str | None = None
    near_duplicate_of: str | None = None
    # (arxiv_id, title) of related earlier papers: the note's citations, or
    # the closest earlier paper when there's no note.
    similar: list[tuple[str, str]] = field(default_factory=list)
    # Near duplicates of this paper in the same section, folded in under it.
    see_also: list["PaperForEmail"] = field(default_factory=list)


@dataclass
class DigestForEmail:
    """One completed digest run, reduced to what the email needs."""

    generated_at: datetime
    overview: str | None
    papers: list[PaperForEmail]


@dataclass
class TopicSection:
    """One topic's part of an email: the papers to show (already capped, newest
    day first and highest-ranked first within a day), overviews of the digest runs they came from, and how many papers
    the cap left out."""

    topic_name: str
    papers: list[PaperForEmail]
    overviews: list[str]
    omitted: int = 0


def build_topic_section(
    topic_name: str, digests: list[DigestForEmail], max_papers: int | None = None
) -> TopicSection | None:
    """Merge a topic's digests into one section, or None if they hold no
    papers at all (empty runs contribute nothing - not even a heading).

    Papers are de-duplicated by arxiv_id and cut to the `max_papers`
    highest-scoring (see app.services.ranking; ties go to the newest). The
    kept papers are then laid out by submission day, newest day first, best
    first within a day. A paper with no submission date falls back to its
    digest's run time, so it still sorts and groups somewhere sensible.
    """
    picked: dict[str, tuple[PaperForEmail, DigestForEmail]] = {}
    for digest in digests:
        for paper in digest.papers:
            if paper.arxiv_id in picked:
                continue
            if paper.published_at is None:
                paper = dataclasses.replace(paper, published_at=digest.generated_at)
            picked[paper.arxiv_id] = (paper, digest)
    if not picked:
        return None
    _fold_near_duplicates(picked)

    candidates = list(picked.values())
    scores = score_papers([p for p, _ in candidates])
    score_of = {p.arxiv_id: s for (p, _), s in zip(candidates, scores)}
    ranked = sorted(
        candidates,
        key=lambda pd: (score_of[pd[0].arxiv_id], pd[0].published_at),
        reverse=True,
    )
    kept = ranked[:max_papers] if max_papers else ranked
    # Stable sort: within a day, equal scores keep their newest-first order.
    kept.sort(key=lambda pd: (_day(pd[0]), score_of[pd[0].arxiv_id]), reverse=True)

    # Overviews only from runs that contributed a shown paper, in run order -
    # an overview describing only capped-out papers would be confusing.
    contributing = {id(d) for _, d in kept}
    overviews: list[str] = []
    for digest in digests:
        if id(digest) in contributing and digest.overview and digest.overview not in overviews:
            overviews.append(digest.overview)

    return TopicSection(
        topic_name=topic_name,
        papers=[p for p, _ in kept],
        overviews=overviews,
        omitted=len(ranked) - len(kept),
    )


def _fold_near_duplicates(picked: dict[str, tuple[PaperForEmail, DigestForEmail]]) -> None:
    """Move each paper whose near-duplicate original is also in this section
    under that original (its `see_also`), in place. Chains (C duplicates B,
    which duplicates A) fold into the first paper. A folded paper isn't
    ranked or counted against the cap - it rides along with its original."""

    def root(arxiv_id: str) -> str:
        seen = {arxiv_id}
        while (parent := picked[arxiv_id][0].near_duplicate_of) in picked and parent not in seen:
            seen.add(parent)
            arxiv_id = parent
        return arxiv_id

    roots = {aid: root(aid) for aid in picked}
    folded = {aid: original for aid, original in roots.items() if original != aid}
    for aid, original in sorted(folded.items()):
        paper, digest = picked[original]
        picked[original] = (dataclasses.replace(paper, see_also=[*paper.see_also, picked[aid][0]]), digest)
    for aid in folded:
        del picked[aid]


_ARXIV_ID = re.compile(r"\b\d{4}\.\d{4,5}\b")


def _linkify_arxiv_ids(escaped: str) -> str:
    return _ARXIV_ID.sub(
        lambda m: f'<a href="https://arxiv.org/abs/{m.group(0)}">{m.group(0)}</a>', escaped
    )


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _day(paper: PaperForEmail) -> date:
    return paper.published_at.astimezone(timezone.utc).date()


def _format_day(d: date) -> str:
    # Not strftime("%-d"): that flag is glibc/BSD-only.
    return f"{d:%B} {d.day}, {d.year}"


def render_digest_email(
    sections: list[TopicSection], *, summaries_enabled: bool = True
) -> DigestEmailContent:
    """Render one email covering every section (one per subscribed topic).
    Callers drop empty sections before calling - see `build_topic_section`."""
    total_papers = sum(len(s.papers) for s in sections)
    names = [s.topic_name for s in sections]
    if len(sections) == 1:
        subject = f"{names[0]}: {_plural(total_papers, 'new paper')}"
    else:
        subject = f"Research digest: {_plural(total_papers, 'new paper')} ({', '.join(names)})"

    text_lines = [f"Your research digest: {', '.join(names)}", ""]
    html_parts = [f"<h1>Your research digest: {escape(', '.join(names))}</h1>"]

    if not summaries_enabled:
        note = (
            "AI-written summaries are currently disabled for this digest - "
            "set SUMMARY_BACKEND=ollama to enable them."
        )
        text_lines += [note, ""]
        html_parts.append(f"<p><em>{escape(note)}</em></p>")

    for section in sections:
        heading = f"{section.topic_name} - {_plural(len(section.papers), 'new paper')}"
        text_lines += ["=" * len(heading), heading, "=" * len(heading), ""]
        html_parts.append(f"<h2>{escape(heading)}</h2>")

        for overview in section.overviews:
            text_lines += [overview, ""]
            html_parts.append(f"<p><em>{escape(overview)}</em></p>")

        # Papers arrive newest day first, so groupby yields each day exactly once.
        for day, papers in groupby(section.papers, key=_day):
            label = _format_day(day)
            text_lines += [label, "-" * len(label)]
            html_parts.append(f"<h3>{escape(label)}</h3>")
            for paper in papers:
                url = f"https://arxiv.org/abs/{paper.arxiv_id}"
                text_lines.append(f"* {paper.title}")
                if paper.summary:
                    text_lines.append(f"  {paper.summary}")
                if paper.related_note:
                    text_lines.append(f"  Related: {paper.related_note}")
                elif paper.similar:
                    sim_id, sim_title = paper.similar[0]
                    text_lines.append(
                        f"  Similar earlier paper: {sim_title} https://arxiv.org/abs/{sim_id}"
                    )
                text_lines.append(f"  {url}")
                for dup in paper.see_also:
                    text_lines.append(f"  Also (near-duplicate): {dup.title} https://arxiv.org/abs/{dup.arxiv_id}")
                text_lines.append("")

                html_parts.append(f"<p><strong>{escape(paper.title)}</strong>")
                if paper.summary:
                    html_parts.append(f"<br>{escape(paper.summary)}")
                if paper.related_note:
                    html_parts.append(
                        f"<br><em>Related:</em> {_linkify_arxiv_ids(escape(paper.related_note))}"
                    )
                elif paper.similar:
                    sim_id, sim_title = paper.similar[0]
                    sim_url = f"https://arxiv.org/abs/{sim_id}"
                    html_parts.append(
                        f'<br><em>Similar earlier paper:</em> <a href="{escape(sim_url, quote=True)}">'
                        f"{escape(sim_title)}</a>"
                    )
                html_parts.append(
                    f'<br><a href="{escape(url, quote=True)}">'
                    f"arxiv.org/abs/{escape(paper.arxiv_id)}</a>"
                )
                for dup in paper.see_also:
                    dup_url = f"https://arxiv.org/abs/{dup.arxiv_id}"
                    html_parts.append(
                        f'<br>Also (near-duplicate): <a href="{escape(dup_url, quote=True)}">'
                        f"{escape(dup.title)}</a>"
                    )
                html_parts.append("</p>")

        if section.omitted:
            more = (
                f"+ {_plural(section.omitted, 'more ' + section.topic_name + ' paper')} "
                f"not shown (showing the top {len(section.papers)})."
            )
            text_lines += [more, ""]
            html_parts.append(f"<p><em>{escape(more)}</em></p>")

    return DigestEmailContent(
        subject=subject,
        text_body="\n".join(text_lines).rstrip() + "\n",
        html_body="\n".join(html_parts),
    )


class EmailSender:
    def __init__(self, settings=None, smtp_client_factory=None) -> None:
        self._settings = settings or get_settings()
        # Defaults to a real smtplib.SMTP connected to the configured host -
        # tests pass a fake factory instead.
        self._smtp_client_factory = smtp_client_factory

    def _make_client(self) -> smtplib.SMTP:
        if self._smtp_client_factory is not None:
            return self._smtp_client_factory()
        return smtplib.SMTP(self._settings.smtp_host, self._settings.smtp_port, timeout=10)

    def send(self, to: str, content: DigestEmailContent) -> None:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = content.subject
        msg["From"] = self._settings.smtp_from_address
        msg["To"] = to
        msg.attach(MIMEText(content.text_body, "plain"))
        msg.attach(MIMEText(content.html_body, "html"))

        with self._make_client() as client:
            if self._settings.smtp_use_tls:
                client.starttls()
            if self._settings.smtp_username:
                client.login(self._settings.smtp_username, self._settings.smtp_password)
            client.sendmail(self._settings.smtp_from_address, [to], msg.as_string())
