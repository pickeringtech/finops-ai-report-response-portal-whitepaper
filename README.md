# AI-Assisted FinOps Report Response Portal

> Whitepaper & Design Document

This repository contains the source and build system for a detailed technical design document proposing a **controlled, AI-assisted workflow** for responding to enterprise FinOps reports inside highly locked-down AWS environments.

## Purpose

Enterprise FinOps teams regularly surface cost anomalies, ownership gaps, and allocation issues. The response process is usually fragmented across email, spreadsheets, and manual follow-ups, with no structured audit trail or consistent data for dashboards.

This design proposes a narrow, secure alternative:

- Users receive a secure deep link in an email for each report item.
- They authenticate via enterprise SSO and enter a **case-specific chat**.
- An AI assistant (powered by Amazon Bedrock) explains the item using a tightly-scoped case file, then guides the user through controlled actions:
  - Submit a justification
  - Challenge the allocation
  - Request reassignment
  - Mark for further investigation
  - Attach evidence
- All writes are validated and performed by the backend; the model never has broad AWS access.
- Approved responses are exported to S3 for dashboarding and analytics.

The system is deliberately **non-autonomous**: no infrastructure changes, no direct catalogue mutation, no raw CUR access, and no self-directed remediation.

## Repository Layout

| Path            | Description |
|-----------------|-------------|
| `adviser.md`    | Complete Markdown source (≈1,300 lines, 32 sections, numerous tables and diagrams) |
| `render.py`     | Custom single-file HTML + PDF renderer with embedded diagrams |
| `package.json`  | Provides `@mermaid-js/mermaid-cli` for build-time diagram rendering |
| `dist/`         | Generated outputs (`adviser.html`, `adviser.pdf`) — gitignored |
| `.gitignore`    | Standard ignores for caches, node_modules, and rendered artefacts |

## Building the Document

### Prerequisites

- Python 3.10 or newer
- Node.js + npm
- Chromium or Google Chrome (headless PDF export)

### One-time Setup

```bash
npm install
```

### Render Commands

```bash
# Full render (HTML + PDF) — default source is adviser.md
python render.py
# or
npm run render

# HTML only (faster iteration)
python render.py --no-pdf

# Custom output names / directory
python render.py adviser.md -o dist \
  --html-name finops-response-portal.html \
  --pdf-name finops-response-portal.pdf
```

The renderer performs several advanced steps at build time:

- Parses GitHub-style tables with column alignment
- Renders Mermaid flowcharts to self-contained SVG using the Mermaid CLI
- Generates custom conversation-diagram and security-architecture SVG diagrams
- Builds a sticky, scroll-aware table of contents
- Produces a single-file, standalone HTML document with professional typography and a branded cover page
- Exports pixel-perfect PDF via Chromium’s print-to-PDF engine

## Document Highlights

The whitepaper covers the full enterprise delivery surface:

- Problem statement and user journey
- Security-centred architecture (application boundary, guardrails, S3-backed stores)
- Detailed networking requirements (VPC endpoints, PrivateLink, cross-account patterns)
- IAM role catalogue and least-privilege runtime permissions
- Security enablement matrix template for platform/security reviews
- Phased delivery plan and risk register
- Explicit “key security statements” for design review conversations

## Viewing the Outputs

- **HTML**: Open `dist/adviser.html` in any modern browser. It is fully self-contained.
- **PDF**: `dist/adviser.pdf` is print-ready and suitable for formal review or distribution.

## Contributing

Edit `adviser.md` directly. After significant changes, re-run the render command to update both artefacts. The custom renderer is intentionally self-contained (standard library + optional Chromium) so the repository remains easy to clone and build in air-gapped or restricted environments.

## Recommended Internal Naming

The document recommends **FinOps Response Portal** (or similar) and explicitly advises against names that imply autonomous agents or broad remediation capabilities.

---

*Part of the PickTech Whitepapers collection.*