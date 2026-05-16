# Stage 2: LLM Features — Archivist, Ghostwriter, Librarian, Public Bot

**Goal**: Build the four AI personas that make Homebound a "living digital persona" rather than a static archive. This is the killer differentiator.

**Depends on**: Stage 1 (multi-user, pgvector, embeddings pipeline)

---

## 2.1 Architecture: RAG Pipeline

All four personas share a single RAG (Retrieval-Augmented Generation) pipeline:

```
User query
    ↓
Generate query embedding (OpenAI text-embedding-3-small)
    ↓
Vector similarity search (pgvector, top-K posts)
    ↓
Construct prompt: system prompt (persona) + retrieved posts + user query
    ↓
LLM inference (Claude Haiku / GPT-4o-mini)
    ↓
Response with citations (links to source posts)
```

### Key design decisions

- **No fine-tuning.** Fine-tuning hallucinates facts and is expensive to maintain. It's useful for capturing writing style/tone but not factual recall. All factual grounding comes from RAG.
- **Model-agnostic.** Abstract the LLM provider behind a simple interface. Swap Claude ↔ GPT ↔ local models without changing application code.
- **Per-user context.** Vector search is always scoped to the current user's posts. No cross-user data leakage.
- **Citation-first.** Every AI response includes links to the source posts it drew from. Builds trust and drives engagement with the archive.

### LLM Provider Configuration

| Tier | Embedding Model | Inference Model | Monthly limit |
| --- | --- | --- | --- |
| Basic ($1/mo) | text-embedding-3-small | — (search only, no chat) | 50 searches |
| Premium ($10/mo) | text-embedding-3-small | Claude Haiku / GPT-4o-mini | 500 queries |
| White Glove ($50/mo) | text-embedding-3-small | Claude Sonnet / GPT-4o | 2000 queries |
| Self-hosted | User's choice | User's choice | Unlimited (user pays) |

---

## 2.2 The Librarian (Private Semantic Search)

### What it does
Natural-language search across the entire archive. "Find that restaurant I posted about in 2016" works even if the user never typed "restaurant" — the embedding captures the semantic meaning.

### Deliverables

1. **Search UI upgrade**: Replace current keyword search with hybrid search:
   - If query looks like keywords → existing full-text search
   - If query looks like natural language → semantic search via pgvector
   - Toggle: user can switch between keyword and semantic modes

2. **Search results page**: Show matched posts with relevance score and highlighted context. Include which source (Google+, Facebook, Twitter) each result came from.

3. **"Vibe" search examples** (show in UI as suggestions):
   - "Posts where I was feeling homesick"
   - "Debugging and programming stories"
   - "Travel photos from 2016"
   - "My stance on social media privacy"

### API

```
GET /api/search/semantic/?q=that+restaurant+in+2016&limit=10
→ [{post_id, title, snippet, source, created_at, similarity_score}, ...]
```

---

## 2.3 The Archivist (Private Memory & Synthesis)

### What it does
Proactive memory features. "On this day" memories, timeline synthesis, cross-platform threading.

### Deliverables

1. **"On This Day" widget**: Dashboard widget showing posts from the same date in previous years. Enhanced with LLM: instead of just listing posts, synthesize a brief narrative ("5 years ago today you were in Berlin, posting about street food and complaining about jet lag").

2. **Cross-platform threading**: During import, identify related posts across platforms (e.g., photo posted on one platform, discussion on another). Uses embedding similarity with a time-window constraint (posts within 48 hours with high semantic similarity are likely related).

3. **Timeline synthesis**: "Summarize my 2016" → LLM reads all posts from that year, produces a narrative with key themes, milestones, and emotional arc. Cite specific posts.

4. **"Debate My Past Self"**: While drafting a new post, hit "Review against my archive." The LLM reads the draft, searches for contradictions or evolution in past opinions. "You said X in 2018 — your view has shifted. Want to acknowledge this?"

### API

```
GET /api/archivist/on-this-day/
GET /api/archivist/synthesize/?year=2016
POST /api/archivist/debate/ {draft_text: "..."}
→ {narrative, cited_posts: [...], contradictions: [...]}
```

---

## 2.4 The Ghostwriter (Private Writing Assistant)

### What it does
Turns scattered social media fragments into polished long-form content, written in the user's historical voice.

### Deliverables

1. **Fragment-to-essay tool**: User selects N posts (or a date range + topic), Ghostwriter stitches them into a cohesive essay. Preserves the user's tone by including style examples in the prompt.

2. **Voice calibration**: On first use, analyze 50+ posts to build a "voice profile" — characteristic phrases, typical sentence length, humor patterns, vocabulary. Store as a system prompt prefix.

3. **Blog post drafting**: "Write a post about my experience moving to the US, based on my 2015-2016 posts." Ghostwriter searches the archive, extracts relevant posts, synthesizes a draft.

4. **Editing assistance**: In the Markdown editor, add a "Ghostwriter" sidebar. User can ask: "Make this more concise," "Add a personal anecdote from my archive," "What else have I written about this topic?"

### Integration with Editor

The existing EasyMDE Markdown editor gets a Ghostwriter panel:
- Chat-style interface in a sidebar
- Ghostwriter can read the current draft and the user's archive
- Suggestions are inserted into the editor with one click
- All interactions stay private (never sent to public bot)

---

## 2.5 The Public Bot ("Talk to the Author")

### What it does
A public-facing chat widget on the user's site. Visitors ask questions, bot answers using the public archive with citations.

### Deliverables

1. **Chat widget**: Embeddable JavaScript widget (like Intercom/Crisp) that appears on the user's public site. Clean, minimal design. Shows "Ask the author's AI" or similar.

2. **Public-only filtering**: The bot ONLY searches posts marked as `visibility=public`. Never surfaces friends-only or private posts. This is a hard security boundary.

3. **Citation links**: Every response includes direct links to the posts the bot drew from. "Based on a post from March 2017: [link]" — drives engagement with the archive.

4. **Rate limiting**: 100 queries/month per site (Basic tier gets no public bot). Widget gracefully hides after limit. White Glove gets 500/month.

5. **Persona prompt**: The bot speaks in third person about the author. "Vladimir wrote about this in 2016..." Not pretending to *be* the author (that's creepy), but presenting the author's public record.

6. **Abuse protection**: Block obviously inappropriate queries. Limit response length. No jailbreaking via prompt injection (the system prompt is hardened).

### Widget Embed Code

```html
<script src="https://homebound.app/widget.js" data-site="vyakunin"></script>
```

Users can toggle the widget on/off from their dashboard (`Site.enable_public_bot`).

---

## 2.6 Media Preprocessing (Magic)

### What it does
Makes opaque media (images, screenshots, videos) fully searchable and SEO-optimized.

### Deliverables

1. **Image embedding via CLIP**: During import, run images through CLIP to generate visual embeddings. Enables natural-language image search: "photos of my kid's drum class," "beach sunset pictures."
   - Store in a separate `MediaEmbedding` model (pgvector)
   - Search endpoint: `/api/search/media/?q=beach+sunset`

2. **Automated alt-text**: Vision model (GPT-4o-mini with vision, or CLIP + caption model) generates descriptive alt-text for every image. Injected into HTML on render. Massive SEO multiplier.

3. **Screenshot OCR**: Detect screenshots (aspect ratio + UI element heuristics), run OCR (Tesseract or cloud OCR), store extracted text in `PostMedia.extracted_text`. Make searchable alongside post text.

4. **Batch processing**: Media preprocessing is expensive and slow. Run as background tasks with priority queue:
   - High priority: alt-text generation (affects SEO immediately)
   - Medium: CLIP embeddings (affects search)
   - Low: OCR (nice-to-have)

### Cost per user

| Feature | Cost per 1000 images | Notes |
| --- | --- | --- |
| CLIP embeddings | ~$0.05 (local model) | Can run locally, no API cost |
| Alt-text (GPT-4o-mini vision) | ~$2.00 | API cost, batched |
| OCR (Tesseract) | Free | Local processing |

White Glove tier includes all preprocessing. Basic/Premium users get alt-text only (cheapest, highest value).

---

## 2.7 MCP Server (Personal Archive for LLM Agents)

### Why this matters more than the web chat UI

Early adopters are LLM enthusiasts. They live inside Claude Desktop, Cursor, or custom agent pipelines. They won't context-switch to a separate web app to query their archive — but they'll use it constantly if it's available as an MCP tool inside their existing workflow.

An MCP server is a thin wrapper over the same RAG pipeline that powers the web features. Low effort, disproportionate value for the power-user segment.

### MCP Tools

| Tool | Description | Example invocation |
| --- | --- | --- |
| `search_posts` | Semantic or keyword search across the archive | "Find posts where I discussed vim keybindings" |
| `get_post` | Retrieve a specific post by ID with full content, media URLs, comments | "Show me post #4521" |
| `on_this_day` | Posts from this date in previous years, optionally with LLM synthesis | "What was I doing on March 31 in past years?" |
| `ghostwrite` | Generate a draft from a topic, pulling relevant archive context | "Draft a post about my experience learning Rust, based on my archive" |
| `search_media` | Natural-language image search via CLIP embeddings | "Photos from Berlin trip" |
| `get_timeline` | Posts from a date range, filtered by source/tag | "All my Google+ posts from 2015 about programming" |

### MCP Resources

| Resource URI | Description |
| --- | --- |
| `archive://profile` | User bio, post counts by source, date range |
| `archive://recent` | Most recent N posts (configurable) |
| `archive://stats` | Archive statistics: posts per year, per source, top tags, word count |
| `archive://sources` | Available sources and their post counts |

### Deployment Models

**Self-hosted** (primary target):
```bash
# stdio mode for Claude Desktop / Cursor
python -m homebound.mcp --db-url postgresql://localhost/personal_blog

# In claude_desktop_config.json:
{
  "mcpServers": {
    "my-archive": {
      "command": "python",
      "args": ["-m", "homebound.mcp", "--db-url", "postgresql://localhost/personal_blog"]
    }
  }
}
```

**Hosted**:
- Authenticated SSE endpoint: `https://username.homebound.app/mcp/`
- API key auth (generated from dashboard)
- Same rate limits as the web chat (tied to tier)

### Implementation

The MCP server reuses the RAG pipeline internals:
- `search_posts` → same vector search as Librarian
- `ghostwrite` → same prompt construction as Ghostwriter
- `on_this_day` → same query as Archivist

New code is essentially: MCP protocol handling (JSON-RPC over stdio/SSE) + tool/resource schema definitions + auth for hosted mode. The Python MCP SDK (`mcp` package) handles the protocol layer.

Estimated effort: **3-5 days** (reuses all existing RAG infrastructure).

### Why this is a marketing asset

"Your social media archive as an MCP server" is a one-line pitch that LLM enthusiasts immediately understand and want. It's demo-able in a tweet-sized screen recording. It differentiates sharply from every competitor.

---

## 2.8 Infrastructure Requirements

### New dependencies
- `pgvector` (already from Stage 1)
- `openai` Python package (embeddings + inference)
- `anthropic` Python package (inference)
- `django-channels` or SSE library (streaming chat responses)
- `Pillow` (image processing, likely already present)
- `pytesseract` + Tesseract binary (OCR)
- `open_clip_torch` or CLIP API (image embeddings)

### Server resources
- LLM inference: API calls, no local GPU needed
- CLIP: CPU-only inference is slow (~1 img/sec) but acceptable for background processing. Consider cloud GPU for large imports.
- Memory: pgvector adds ~6KB per post for 1536-dim embeddings. 10k posts ≈ 60 MB. Manageable.

### API key management
- Per-platform: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`
- Per-user (self-hosted): stored in user's `.env`
- Hosted: platform keys with per-user usage tracking and rate limiting

---

## Exit Criteria

- [ ] Semantic search returns relevant results for natural-language queries
- [ ] "On This Day" widget shows synthesized memories
- [ ] Ghostwriter can turn selected posts into a cohesive essay
- [ ] Public bot answers visitor questions with citations to public posts
- [ ] Public bot never surfaces private/friends-only posts
- [ ] Rate limiting works correctly per tier
- [ ] Media preprocessing generates alt-text for imported images
- [ ] CLIP-based image search finds relevant images for natural-language queries
- [ ] Chat interface streams responses (not blocking)
- [ ] All features scoped to per-user data (no cross-user leakage)
- [ ] MCP server works in stdio mode (Claude Desktop, Cursor)
- [ ] MCP `search_posts` returns relevant results via semantic search
- [ ] MCP `ghostwrite` produces coherent drafts with archive context
- [ ] Hosted MCP endpoint authenticates via API key
- [ ] `bazel test //tests:...` passes with LLM feature tests (mocked API calls)

---

## Estimated Effort

| Component | Estimate |
| --- | --- |
| RAG pipeline (shared infrastructure) | 1 week |
| Librarian (semantic search UI) | 3-4 days |
| Archivist (on-this-day, synthesis, debate) | 1-2 weeks |
| Ghostwriter (fragment-to-essay, editor integration) | 1-2 weeks |
| Public bot (widget, rate limiting, abuse protection) | 1 week |
| Media preprocessing (CLIP, alt-text, OCR) | 1-2 weeks |
| MCP server (stdio + hosted endpoint) | 3-5 days |
| Testing + polish | 1 week |
| **Total** | **7-10 weeks** |

---

## Cleanup (Post-Implementation)

- [ ] Document RAG pipeline architecture in HIGH_LEVEL_DESIGN.md
- [ ] Create rule for LLM prompt engineering patterns
- [ ] Update DEPLOYMENT_DESIGN.md with new dependencies
- [ ] Delete this planning doc or convert to current-state doc
