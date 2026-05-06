"""
Generates market open/close reports as PDF and sends summaries to Telegram.
"""

from datetime import datetime
from pathlib import Path

import yfinance as yf
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

import config
from ig_client import IGClient
from utils import send_telegram, send_telegram_document, log

INDICES = {
    "S&P 500":      "^GSPC",
    "NASDAQ":       "^IXIC",
    "Dow Jones":    "^DJI",
    "Russell 2000": "^RUT",
    "VIX":          "^VIX",
}

SECTORS = {
    "Technology":    "XLK",
    "Financials":    "XLF",
    "Energy":        "XLE",
    "Healthcare":    "XLV",
    "Industrials":   "XLI",
    "Consumer Disc": "XLY",
    "Consumer Stap": "XLP",
    "Utilities":     "XLU",
    "Real Estate":   "XLRE",
    "Materials":     "XLB",
}

REPORT_DIR = Path("reports")
REPORT_DIR.mkdir(exist_ok=True)


def gather_account_snapshot(client: IGClient) -> dict:
    acc          = client.get_preferred_account()
    balance      = float(acc["balance"]["balance"])
    available    = float(acc["balance"]["available"])
    pnl          = float(acc["balance"]["profitLoss"])
    currency     = acc["currency"]
    positions    = client.get_positions()
    transactions = [t for t in client.get_transactions(max_span_seconds=86400)
                    if t.get("epic") == config.IG_EPIC]

    return {
        "equity":       balance,
        "cash":         available,
        "pnl":          pnl,
        "currency":     currency,
        "positions":    positions,
        "transactions": transactions,
        "timestamp":    datetime.now(),
    }


def gather_market_data() -> dict:
    """Fetch latest index prices and sector performance from Yahoo Finance."""
    def _fetch(ticker: str) -> dict | None:
        try:
            hist = yf.Ticker(ticker).history(period="2d", interval="1d", prepost=True)
            if len(hist) < 2:
                return None
            prev  = float(hist["Close"].iloc[-2])
            last  = float(hist["Close"].iloc[-1])
            pct   = (last - prev) / prev * 100
            return {"price": last, "prev": prev, "pct": pct}
        except Exception:
            return None

    indices = {name: _fetch(sym) for name, sym in INDICES.items()}
    sectors = {name: _fetch(sym) for name, sym in SECTORS.items()}
    return {
        "indices": {k: v for k, v in indices.items() if v},
        "sectors": {k: v for k, v in sectors.items() if v},
    }


def build_report_pdf(snapshot: dict, report_type: str, output_path: Path, market: dict | None = None) -> None:
    doc = SimpleDocTemplate(
        str(output_path), pagesize=letter,
        rightMargin=0.75 * inch, leftMargin=0.75 * inch,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
    )

    styles      = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "Title", parent=styles["Heading1"],
        fontSize=22, textColor=colors.HexColor("#1a365d"),
        spaceAfter=12, alignment=1,
    )
    subtitle_style = ParagraphStyle(
        "Subtitle", parent=styles["Normal"],
        fontSize=11, textColor=colors.grey, spaceAfter=20, alignment=1,
    )
    section_style = ParagraphStyle(
        "Section", parent=styles["Heading2"],
        fontSize=14, textColor=colors.HexColor("#2c5282"),
        spaceAfter=8, spaceBefore=14,
    )
    normal = styles["Normal"]

    story = []
    ts  = snapshot["timestamp"]
    cur = snapshot["currency"]

    title = "Market Open Report" if report_type == "open" else "Market Close Report"
    story.append(Paragraph(f"📊 {title}", title_style))
    story.append(Paragraph(
        f"{ts.strftime('%A, %B %d, %Y')} &nbsp;&nbsp;|&nbsp;&nbsp; Generated at {ts.strftime('%I:%M %p')}",
        subtitle_style,
    ))

    # Account summary
    equity = snapshot["equity"]
    cash   = snapshot["cash"]
    pnl    = snapshot["pnl"]
    pl_color = colors.green if pnl >= 0 else colors.red
    pl_sign  = "+" if pnl >= 0 else ""

    story.append(Paragraph("Account Summary", section_style))
    summary_table = Table(
        [
            ["Balance",          f"{cur} {equity:,.2f}"],
            ["Available Funds",  f"{cur} {cash:,.2f}"],
            ["P/L",              f"{pl_sign}{cur} {pnl:,.2f}"],
        ],
        colWidths=[2.5 * inch, 3 * inch],
    )
    summary_table.setStyle(TableStyle([
        ("BACKGROUND",  (0, 0), (0, -1), colors.HexColor("#edf2f7")),
        ("TEXTCOLOR",   (1, 2), (1, 2),  pl_color),
        ("FONTNAME",    (0, 0), (-1, -1), "Helvetica"),
        ("FONTNAME",    (1, 2), (1, 2),  "Helvetica-Bold"),
        ("FONTSIZE",    (0, 0), (-1, -1), 11),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING",    (0, 0), (-1, -1), 8),
        ("GRID",          (0, 0), (-1, -1), 0.5, colors.lightgrey),
    ]))
    story.append(summary_table)

    # Market overview
    if market:
        def _arrow(pct): return "▲" if pct >= 0 else "▼"
        def _color(pct): return colors.HexColor("#276749") if pct >= 0 else colors.HexColor("#c53030")

        if market.get("indices"):
            story.append(Paragraph("Market Overview", section_style))
            idx_data = [["Index", "Price", "Change"]]
            for name, d in market["indices"].items():
                idx_data.append([name, f"{d['price']:,.2f}", f"{_arrow(d['pct'])} {d['pct']:+.2f}%"])
            idx_table = Table(idx_data, colWidths=[2.2 * inch, 2 * inch, 2 * inch])
            idx_style = [
                ("BACKGROUND",    (0, 0), (-1, 0), colors.HexColor("#2c5282")),
                ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
                ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE",      (0, 0), (-1, -1), 11),
                ("ALIGN",         (1, 0), (-1, -1), "RIGHT"),
                ("GRID",          (0, 0), (-1, -1), 0.5, colors.lightgrey),
                ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, colors.HexColor("#f7fafc")]),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING",    (0, 0), (-1, -1), 7),
            ]
            for i, (_, d) in enumerate(market["indices"].items(), start=1):
                idx_style.append(("TEXTCOLOR", (2, i), (2, i), _color(d["pct"])))
                idx_style.append(("FONTNAME",  (2, i), (2, i), "Helvetica-Bold"))
            idx_table.setStyle(TableStyle(idx_style))
            story.append(idx_table)

        if market.get("sectors"):
            story.append(Paragraph("Sector Performance", section_style))
            sec_data = [["Sector", "Price", "Change"]]
            for name, d in market["sectors"].items():
                sec_data.append([name, f"{d['price']:.2f}", f"{_arrow(d['pct'])} {d['pct']:+.2f}%"])
            sec_table = Table(sec_data, colWidths=[2.2 * inch, 2 * inch, 2 * inch])
            sec_style = [
                ("BACKGROUND",    (0, 0), (-1, 0), colors.HexColor("#2c5282")),
                ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
                ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE",      (0, 0), (-1, -1), 10),
                ("ALIGN",         (1, 0), (-1, -1), "RIGHT"),
                ("GRID",          (0, 0), (-1, -1), 0.5, colors.lightgrey),
                ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, colors.HexColor("#f7fafc")]),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING",    (0, 0), (-1, -1), 6),
            ]
            for i, (_, d) in enumerate(market["sectors"].items(), start=1):
                sec_style.append(("TEXTCOLOR", (2, i), (2, i), _color(d["pct"])))
                sec_style.append(("FONTNAME",  (2, i), (2, i), "Helvetica-Bold"))
            sec_table.setStyle(TableStyle(sec_style))
            story.append(sec_table)

    # Open positions
    story.append(Paragraph("Open Positions", section_style))
    positions = snapshot["positions"]
    if positions:
        pos_data = [["Instrument", "Direction", "Size", "Open", "Current", "P/L"]]
        for p in positions:
            pos  = p["position"]
            mkt  = p["market"]
            direction  = pos["direction"]
            size       = float(pos["size"])
            open_level = float(pos["level"])
            bid        = float(mkt.get("bid", 0))
            offer      = float(mkt.get("offer", 0))
            current    = bid if direction == "BUY" else offer
            pnl_pos    = (current - open_level) * size if direction == "BUY" else (open_level - current) * size
            sign       = "+" if pnl_pos >= 0 else ""
            pos_data.append([
                mkt["instrumentName"][:24],
                direction,
                f"{size:g}",
                f"{open_level:.2f}",
                f"{current:.2f}",
                f"{sign}{pnl_pos:,.2f}",
            ])
        pos_table = Table(pos_data, colWidths=[2 * inch, 0.8 * inch, 0.7 * inch, 1 * inch, 1 * inch, 1.2 * inch])
        pos_table.setStyle(TableStyle([
            ("BACKGROUND",  (0, 0), (-1, 0), colors.HexColor("#2c5282")),
            ("TEXTCOLOR",   (0, 0), (-1, 0), colors.white),
            ("FONTNAME",    (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",    (0, 0), (-1, -1), 10),
            ("ALIGN",       (2, 0), (-1, -1), "RIGHT"),
            ("GRID",        (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7fafc")]),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ]))
        story.append(pos_table)
    else:
        story.append(Paragraph("<i>No open positions</i>", normal))

    # Today's trades
    story.append(Paragraph("Today's Trades", section_style))
    transactions = snapshot["transactions"]
    if transactions:
        trade_data = [["Date/Time", "Type", "Instrument", "Size", "Open", "Close", "P/L"]]
        for t in transactions:
            trade_data.append([
                str(t.get("dateUtc", t.get("date", "")))[:16],
                t.get("transactionType", ""),
                str(t.get("instrumentName", ""))[:20],
                str(t.get("size", "")),
                str(t.get("openLevel", "")),
                str(t.get("closeLevel", "")),
                str(t.get("profitAndLoss", "")),
            ])
        trade_table = Table(
            trade_data,
            colWidths=[1.1 * inch, 0.8 * inch, 1.5 * inch, 0.6 * inch, 0.8 * inch, 0.8 * inch, 1 * inch],
        )
        trade_table.setStyle(TableStyle([
            ("BACKGROUND",  (0, 0), (-1, 0), colors.HexColor("#2c5282")),
            ("TEXTCOLOR",   (0, 0), (-1, 0), colors.white),
            ("FONTNAME",    (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",    (0, 0), (-1, -1), 9),
            ("ALIGN",       (3, 1), (-1, -1), "RIGHT"),
            ("GRID",        (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7fafc")]),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ]))
        story.append(trade_table)
    else:
        story.append(Paragraph("<i>No trades today</i>", normal))

    story.append(Spacer(1, 0.4 * inch))
    story.append(Paragraph(
        f"Generated by your personal trading bot · IG Markets {config.IG_ACCOUNT_TYPE} account · "
        "This report is for informational purposes only.",
        ParagraphStyle("Footer", parent=normal, fontSize=8, textColor=colors.grey, alignment=1),
    ))

    doc.build(story)


def build_telegram_summary(snapshot: dict, report_type: str, market: dict | None = None) -> str:
    equity = snapshot["equity"]
    cash   = snapshot["cash"]
    pnl    = snapshot["pnl"]
    cur    = snapshot["currency"]
    sign   = "+" if pnl >= 0 else ""
    emoji  = "🟢" if pnl >= 0 else "🔴"

    title = "🌅 <b>Market Open Report</b>" if report_type == "open" else "🌆 <b>Market Close Report</b>"
    ts    = snapshot["timestamp"].strftime("%Y-%m-%d %H:%M")

    msg  = f"{title}\n<i>{ts}</i>\n\n"

    if market:
        def _e(pct): return "🟢" if pct >= 0 else "🔴"
        if market.get("indices"):
            msg += "📈 <b>Market Overview</b>\n"
            for name, d in market["indices"].items():
                msg += f"  {_e(d['pct'])} <b>{name}:</b> {d['price']:,.2f}  ({d['pct']:+.2f}%)\n"
            msg += "\n"
        if market.get("sectors"):
            sorted_sec = sorted(market["sectors"].items(), key=lambda x: x[1]["pct"], reverse=True)
            msg += "🏭 <b>Sectors</b>\n"
            for name, d in sorted_sec:
                msg += f"  {_e(d['pct'])} {name}: {d['pct']:+.2f}%\n"
            msg += "\n"

    msg += f"💰 <b>Balance:</b> {cur} {equity:,.2f}\n"
    msg += f"💵 <b>Available:</b> {cur} {cash:,.2f}\n"
    msg += f"{emoji} <b>P/L:</b> {sign}{cur} {pnl:,.2f}\n\n"

    positions = snapshot["positions"]
    if positions:
        msg += f"📊 <b>Open Positions ({len(positions)}):</b>\n"
        for p in positions[:5]:
            pos        = p["position"]
            mkt        = p["market"]
            direction  = pos["direction"]
            size       = float(pos["size"])
            open_level = float(pos["level"])
            bid        = float(mkt.get("bid", 0))
            offer      = float(mkt.get("offer", 0))
            current    = bid if direction == "BUY" else offer
            pnl_pos    = (current - open_level) * size if direction == "BUY" else (open_level - current) * size
            psign      = "+" if pnl_pos >= 0 else ""
            msg += f"  • {mkt['instrumentName']}: {direction} {size:g} | P/L: {psign}{pnl_pos:,.2f}\n"
        if len(positions) > 5:
            msg += f"  <i>...and {len(positions) - 5} more (see PDF)</i>\n"
    else:
        msg += "📊 <b>No open positions</b>\n"

    msg += f"\n🔄 <b>Trades today:</b> {len(snapshot['transactions'])}\n"
    msg += "\n📄 Full PDF report attached."
    return msg


def generate_report(report_type: str = "close", client: IGClient = None) -> Path:
    log.info(f"Generating {report_type} report...")
    if client is None:
        client = IGClient()
        client.login()

    snapshot = gather_account_snapshot(client)

    market = None
    if report_type == "open":
        log.info("Fetching market data from Yahoo Finance...")
        market = gather_market_data()

    date_str = snapshot["timestamp"].strftime("%Y-%m-%d")
    pdf_path = REPORT_DIR / f"{date_str}_{report_type}_report.pdf"

    build_report_pdf(snapshot, report_type, pdf_path, market=market)
    log.info(f"PDF saved: {pdf_path}")

    send_telegram(build_telegram_summary(snapshot, report_type, market=market))
    send_telegram_document(pdf_path, caption=f"📄 {report_type.title()} report")

    return pdf_path


if __name__ == "__main__":
    import sys
    generate_report(sys.argv[1] if len(sys.argv) > 1 else "close")
