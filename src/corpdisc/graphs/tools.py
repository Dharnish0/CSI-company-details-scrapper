"""Read-only tool handlers handed to the ToolBroker (composition root side, so agents never see a DB/FS handle)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from corpdisc.agents.broker import ToolSpec
from corpdisc.db.repos.pages import PageRepo
from corpdisc.services.pagecache import PageCache


class PageExcerptArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_id: int = Field(ge=1)
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=1500, ge=1, le=3000)


def make_page_excerpt_spec(pages: PageRepo, cache: PageCache, run_id: int) -> ToolSpec:
    """``get_page_excerpt``: a bounded window of an already-fetched page of THIS run. Never fetches anything."""

    def handler(args: BaseModel) -> dict:
        a = PageExcerptArgs.model_validate(args.model_dump())
        row = pages.get_page_by_id(a.page_id)
        if row is None or row.run_id != run_id or not row.text_path:
            raise LookupError("page not available")
        text = cache.read_text(row.text_path)
        if text is None:
            raise LookupError("page text unavailable")
        window = text[a.offset : a.offset + a.length]
        return {"page_id": row.page_id, "url": row.canonical_url, "offset": a.offset, "text": window,
                "is_truncated": a.offset + a.length < len(text)}

    return ToolSpec(name="get_page_excerpt", description="Read a bounded window of an already-fetched page.",
                    args_model=PageExcerptArgs, handler=handler, read_only=True, max_result_chars=3500)
