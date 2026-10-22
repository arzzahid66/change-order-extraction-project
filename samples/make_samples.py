"""Generate the synthetic test set (samples/docs/*) and its gold labels (samples/gold/*.json).

Each document exercises a different real-world failure mode. Gold values are
what a careful PM would enter into the system.
"""
from __future__ import annotations

import json
from email.message import EmailMessage
from pathlib import Path

import pymupdf

ROOT = Path(__file__).parent
DOCS, GOLD = ROOT / "docs", ROOT / "gold"

CSS = """
body { font-family: helvetica; font-size: 10px; }
h1 { font-size: 15px; margin: 0 0 6px 0; }
table { border-collapse: collapse; width: 100%; }
td, th { border: 1px solid #444; padding: 3px; }
.r { text-align: right; }
"""


def lines_table(rows: list[tuple]) -> str:
    head = "<tr><th>Cost Code</th><th>Description</th><th>Qty</th><th>Unit</th><th>Unit Price</th><th>Amount</th></tr>"
    body = "".join(
        f"<tr><td>{c}</td><td>{d}</td><td class=r>{q}</td><td>{u}</td><td class=r>{p}</td><td class=r>{a}</td></tr>"
        for c, d, q, u, p, a in rows
    )
    return f"<table>{head}{body}</table>"


def pdf_from_html(pages: list[str], path: Path, scanned: bool = False) -> bytes:
    doc = pymupdf.open()
    for html in pages:
        page = doc.new_page(width=612, height=792)
        page.insert_htmlbox(pymupdf.Rect(40, 40, 572, 752), html, css=CSS)
    if scanned:  # rasterize: no text layer, slight blur from low DPI
        out = pymupdf.open()
        for page in doc:
            pix = page.get_pixmap(dpi=110, colorspace=pymupdf.csGRAY)
            p = out.new_page(width=612, height=792)
            p.insert_image(p.rect, pixmap=pix, rotate=0)
        doc = out
    data = doc.tobytes(garbage=3, deflate=True)
    path.write_bytes(data)
    return data


def gold(name: str, cos: list[dict]) -> None:
    (GOLD / f"{name}.json").write_text(json.dumps({"change_orders": cos}, indent=2))


def form(co: str, project: str, proj_no: str, dt: str, contractor: str, owner: str,
         desc: str, rows: list[tuple], totals: list[tuple[str, str]], schedule: str,
         signature: str, refs: str = "") -> str:
    tot = "".join(f"<tr><td>{k}</td><td class=r>{v}</td></tr>" for k, v in totals)
    return f"""
<h1>CHANGE ORDER # {co}</h1>
<p>Project: {project}<br>Project No: {proj_no}<br>Date: {dt}<br>
Contractor: {contractor}<br>Owner: {owner}</p>
<p><b>Description of Change:</b> {desc}</p>
{f'<p>Reference: {refs}</p>' if refs else ''}
{lines_table(rows) if rows else ''}
<br><table>{tot}</table>
<p>Schedule Impact: {schedule}</p>
<p>{signature}</p>"""


def main() -> None:
    DOCS.mkdir(exist_ok=True)
    GOLD.mkdir(exist_ok=True)

    # 1. Clean, typed form with line items that reconcile.
    pdf_from_html([form(
        "007", "Riverside Medical Office Building", "2024-118", "March 14, 2026",
        "Apex Builders LLC", "Riverside Health Partners",
        "Add duplex receptacles and dedicated 20A circuits in exam rooms 104-107 per RFI-042.",
        [("26 05 19", "Duplex receptacles 20A", "4", "EA", "$185.00", "$740.00"),
         ("26 05 33", "Dedicated 20A circuit", "2", "EA", "$650.00", "$1,300.00"),
         ("09 29 00", "Drywall patch and paint", "1", "LS", "$420.00", "$420.00")],
        [("Subtotal", "$2,460.00"), ("Overhead & Profit (10%)", "$246.00"),
         ("Total Change Order Amount", "$2,706.00")],
        "2 calendar days", "Approved By: Dana Whitfield, Owner's Rep - 03/16/2026", "RFI-042",
    )], DOCS / "co_01_clean_form.pdf")
    gold("co_01_clean_form", [dict(
        change_order_number="007", project_name="Riverside Medical Office Building",
        project_number="2024-118", date_issued="2026-03-14", contractor="Apex Builders LLC",
        owner="Riverside Health Partners", reason="owner_request", status="approved",
        subtotal=2460.0, markup_amount=246.0, tax_amount=None, total_amount=2706.0,
        schedule_impact_days=2, approved_by="Dana Whitfield", references=["RFI-042"], n_line_items=3)])

    # 2. Informal email body - no form, numbers in prose.
    (DOCS / "co_02_email.txt").write_text("""From: Mike Torres <mike@ridgelinebuild.com>
To: Sarah Chen <schen@harborviewdev.com>
Date: Tue, 7 Apr 2026 08:12:45 -0700
Subject: CO 12 - lobby ceiling framing - Harbor View Apartments

Sarah,

Once we opened up the lobby ceiling we found rotted joists along the east wall
(see photos). We need to sister 6 joists and replace the hangers before drywall.

Labor + material comes to $3,850, plus our 15% markup ($577.50), so $4,427.50 total.
This probably pushes the lobby finish by 3 working days.

Please get owner sign-off so we can keep moving.

Thanks,
Mike Torres
Ridgeline Construction
""", encoding="utf-8")
    gold("co_02_email", [dict(
        change_order_number="12", project_name="Harbor View Apartments", project_number=None,
        date_issued="2026-04-07", contractor="Ridgeline Construction", owner=None,
        reason="unforeseen_condition", status="proposed", subtotal=3850.0, markup_amount=577.5,
        tax_amount=None, total_amount=4427.5, schedule_impact_days=3, approved_by=None,
        references=[], n_line_items=0)])

    # 3. Value-engineering credit with parenthesized negatives.
    pdf_from_html([form(
        "3", "Northgate Library Renovation", "NL-2207", "02/09/2026",
        "Summit Interiors Inc.", "City of Northgate",
        "Value engineering: substitute LVT for porcelain tile in corridors 1A-1D.",
        [("09 30 00", "Delete porcelain tile", "4200", "SF", "$11.00", "($46,200.00)"),
         ("09 65 19", "Add LVT flooring", "4200", "SF", "$8.00", "$33,600.00")],
        [("Subtotal", "($12,600.00)"), ("Markup credit (5%)", "($630.00)"),
         ("Total Change Order Amount", "($13,230.00)")],
        "No change to contract time", "Accepted By: Robert Alvarez, City of Northgate",
    )], DOCS / "co_03_credit.pdf")
    gold("co_03_credit", [dict(
        change_order_number="3", project_name="Northgate Library Renovation", project_number="NL-2207",
        date_issued="2026-02-09", contractor="Summit Interiors Inc.", owner="City of Northgate",
        reason="value_engineering", status="approved", subtotal=-12600.0, markup_amount=-630.0,
        tax_amount=None, total_amount=-13230.0, schedule_impact_days=0, approved_by="Robert Alvarez",
        references=[], n_line_items=2)])

    # 4. Scanned form - no text layer at all.
    pdf_from_html([form(
        "18", "Lakeside Distribution Center", "LDC-310", "May 5, 2026",
        "Keystone Concrete Co.", "Lakeside Logistics LLC",
        "Thicken slab at rack column bases per structural bulletin SB-6.",
        [("03 30 00", "Additional concrete 4000 psi", "38", "CY", "$210.00", "$7,980.00"),
         ("03 21 00", "Additional rebar", "2.5", "TON", "$1,800.00", "$4,500.00")],
        [("Subtotal", "$12,480.00"), ("OH&P (12%)", "$1,497.60"), ("Total", "$13,977.60")],
        "4 days", "Status: Pending owner approval", "SB-6",
    )], DOCS / "co_04_scanned.pdf", scanned=True)
    gold("co_04_scanned", [dict(
        change_order_number="18", project_name="Lakeside Distribution Center", project_number="LDC-310",
        date_issued="2026-05-05", contractor="Keystone Concrete Co.", owner="Lakeside Logistics LLC",
        reason="design_change", status="pending", subtotal=12480.0, markup_amount=1497.6,
        tax_amount=None, total_amount=13977.6, schedule_impact_days=4, approved_by=None,
        references=["SB-6"], n_line_items=2)])

    # 5. Arithmetic does not reconcile: lines sum to 5,200 but subtotal says 5,400.
    pdf_from_html([form(
        "22", "Oakmont High School Gym", "OHS-55", "June 1, 2026",
        "Brightline Electric", "Oakmont Unified School District",
        "Relocate gym scoreboard feed and add two emergency lighting units.",
        [("26 05 00", "Relocate scoreboard feed", "1", "LS", "$3,400.00", "$3,400.00"),
         ("26 52 00", "Emergency lighting unit", "2", "EA", "$900.00", "$1,800.00")],
        [("Subtotal", "$5,400.00"), ("Markup (10%)", "$540.00"), ("Sales Tax", "$0.00"),
         ("Total Change Order Amount", "$5,940.00")],
        "1 day", "Status: Proposed", "",
    )], DOCS / "co_05_math_mismatch.pdf")
    gold("co_05_math_mismatch", [dict(
        change_order_number="22", project_name="Oakmont High School Gym", project_number="OHS-55",
        date_issued="2026-06-01", contractor="Brightline Electric", owner="Oakmont Unified School District",
        reason="owner_request", status="proposed", subtotal=5400.0, markup_amount=540.0, tax_amount=0.0,
        total_amount=5940.0, schedule_impact_days=1, approved_by=None, references=[], n_line_items=2,
        expect_review=True)])

    # 6. Two change orders in one document (a CO log export).
    pdf_from_html([
        form("14", "Pine Street Mixed-Use", "PS-901", "July 8, 2026", "Granite Peak Builders",
             "Pine Street Partners LP", "Upgrade storefront glazing to laminated glass per ASI-9.",
             [("08 41 13", "Laminated glass upgrade", "620", "SF", "$14.50", "$8,990.00")],
             [("Subtotal", "$8,990.00"), ("OH&P (10%)", "$899.00"), ("Total", "$9,889.00")],
             "0 days", "Approved By: Lena Brooks", "ASI-9"),
        form("15", "Pine Street Mixed-Use", "PS-901", "July 15, 2026", "Granite Peak Builders",
             "Pine Street Partners LP", "Owner-requested addition of 3 floor boxes in retail unit B.",
             [("26 27 26", "Floor box with poke-through", "3", "EA", "$1,150.00", "$3,450.00")],
             [("Subtotal", "$3,450.00"), ("OH&P (10%)", "$345.00"), ("Total", "$3,795.00")],
             "2 days", "Status: Pending"),
    ], DOCS / "co_06_log.pdf")
    common = dict(project_name="Pine Street Mixed-Use", project_number="PS-901",
                  contractor="Granite Peak Builders", owner="Pine Street Partners LP", tax_amount=None)
    gold("co_06_log", [
        dict(common, change_order_number="14", date_issued="2026-07-08", reason="design_change",
             status="approved", subtotal=8990.0, markup_amount=899.0, total_amount=9889.0,
             schedule_impact_days=0, approved_by="Lena Brooks", references=["ASI-9"], n_line_items=1),
        dict(common, change_order_number="15", date_issued="2026-07-15", reason="owner_request",
             status="pending", subtotal=3450.0, markup_amount=345.0, total_amount=3795.0,
             schedule_impact_days=2, approved_by=None, references=[], n_line_items=1),
    ])

    # 7. Email with the CO attached as a PDF (how Sledge says COs actually arrive).
    att = pdf_from_html([form(
        "PCO-21", "Cedar Ridge Townhomes Phase 2", "CRT-2", "August 3, 2026",
        "Bluewater Plumbing LLC", "Cedar Ridge Homes",
        "Reroute sanitary main around unforeseen ledge rock at Bldg 4.",
        [("22 13 00", "Reroute 6in sanitary main", "85", "LF", "$96.00", "$8,160.00"),
         ("31 23 16", "Rock excavation", "1", "LS", "$2,750.00", "$2,750.00")],
        [("Subtotal", "$10,910.00"), ("Markup (10%)", "$1,091.00"), ("Tax (6%)", "$654.60"),
         ("Total Change Order Amount", "$12,655.60")],
        "5 working days", "Status: Pending GC review",
    )], DOCS / "_attachment.pdf")
    (DOCS / "_attachment.pdf").unlink()
    msg = EmailMessage()
    msg["From"] = "Ops <ops@bluewaterplumbing.com>"
    msg["To"] = "pm@cedarridgehomes.com"
    msg["Date"] = "Mon, 3 Aug 2026 16:40:00 -0400"
    msg["Subject"] = "Pricing for PCO-21 (Bldg 4 sanitary reroute)"
    msg.set_content("Hi, attached is our pricing for the ledge rock reroute at Building 4.\n"
                    "Need an answer by Friday to hold the crew.\n\n-- Bluewater Plumbing")
    msg.add_attachment(att, maintype="application", subtype="pdf", filename="PCO-21.pdf")
    (DOCS / "co_07_email_attachment.eml").write_bytes(bytes(msg))
    gold("co_07_email_attachment", [dict(
        change_order_number="PCO-21", project_name="Cedar Ridge Townhomes Phase 2", project_number="CRT-2",
        date_issued="2026-08-03", contractor="Bluewater Plumbing LLC", owner="Cedar Ridge Homes",
        reason="unforeseen_condition", status="pending", subtotal=10910.0, markup_amount=1091.0,
        tax_amount=654.6, total_amount=12655.6, schedule_impact_days=5, approved_by=None,
        references=[], n_line_items=2)])

    # 8. Bad OCR output: O/0 and l/1 swaps, broken words, no markup line.
    (DOCS / "co_08_ocr_noise.txt").write_text("""WESTGATE C0NSTRUCTI0N GR0UP
C.O. No: 9          Date: 6/2/26
Proj ect: Westgate Elementary Gym Addition     Job # WEG-44
Owner: Westgate School Board

Descripti0n: Add acoustic wall panels to gym per architect ASI-3

Acoustic panels      240  SF   $28.00    $6,720.00
Install labor        1    LS   $1,450.00 $1,450.00

Sub-total      $8,17O.OO
Markup         $745.OO
T0TAL: $8,9l5.OO

Schedule: 3 days
Approved by: ______________
""", encoding="utf-8")
    gold("co_08_ocr_noise", [dict(
        change_order_number="9", project_name="Westgate Elementary Gym Addition", project_number="WEG-44",
        date_issued="2026-06-02", contractor="Westgate Construction Group", owner="Westgate School Board",
        reason="design_change", status="proposed", subtotal=8170.0, markup_amount=745.0, tax_amount=None,
        total_amount=8915.0, schedule_impact_days=3, approved_by=None, references=["ASI-3"],
        n_line_items=2)])

    # 9. Phone photo of a printed CO (JPEG, no PDF at all).
    page_doc = pymupdf.open()
    page = page_doc.new_page(width=612, height=792)
    page.insert_htmlbox(pymupdf.Rect(40, 40, 572, 752), form(
        "41", "Elm Street Fire Station", "ESF-12", "September 9, 2026",
        "Redline Mechanical Inc.", "Town of Elm Grove",
        "Add exhaust fan in apparatus bay per fire marshal comment.",
        [("23 34 00", "Exhaust fan EF-3", "1", "EA", "$4,600.00", "$4,600.00"),
         ("23 31 00", "Ductwork", "60", "LF", "$45.00", "$2,700.00")],
        [("Subtotal", "$7,300.00"), ("Markup (10%)", "$730.00"), ("Total", "$8,030.00")],
        "3 days", "Status: Pending",
    ), css=CSS)
    (DOCS / "co_09_phone_photo.jpg").write_bytes(
        page.get_pixmap(dpi=100, colorspace=pymupdf.csGRAY).tobytes("jpg", jpg_quality=55))
    gold("co_09_phone_photo", [dict(
        change_order_number="41", project_name="Elm Street Fire Station", project_number="ESF-12",
        date_issued="2026-09-09", contractor="Redline Mechanical Inc.", owner="Town of Elm Grove",
        reason="code_requirement", status="pending", subtotal=7300.0, markup_amount=730.0,
        tax_amount=None, total_amount=8030.0, schedule_impact_days=3, approved_by=None,
        references=[], n_line_items=2)])

    # 10. Mixed PDF: typed cover page, but the pricing page is a scan.
    cover = """<h1>CHANGE ORDER # 31</h1>
<p>Project: Bayview Senior Living<br>Project No: BSL-77<br>Date: October 2, 2026<br>
Contractor: Harbor Line Builders<br>Owner: Bayview Care Group</p>
<p><b>Description of Change:</b> Owner-requested upgrade of corridor handrails to stainless steel.</p>
<p>Pricing: see attached signed pricing sheet (page 2).</p>"""
    pricing = f"""<h1>PRICING SHEET - CO # 31</h1>
{lines_table([("05 52 00", "Stainless handrail", "320", "LF", "$62.00", "$19,840.00"),
              ("05 52 00", "Remove existing handrail", "320", "LF", "$6.00", "$1,920.00")])}
<br><table><tr><td>Subtotal</td><td class=r>$21,760.00</td></tr>
<tr><td>OH&amp;P (8%)</td><td class=r>$1,740.80</td></tr>
<tr><td>Total Change Order Amount</td><td class=r>$23,500.80</td></tr></table>
<p>Schedule Impact: 6 days</p><p>Approved By: Grace Okafor, Bayview Care Group</p>"""
    scan_src = pymupdf.open()
    sp = scan_src.new_page(width=612, height=792)
    sp.insert_htmlbox(pymupdf.Rect(40, 40, 572, 752), pricing, css=CSS)
    mixed = pymupdf.open()
    mixed.new_page(width=612, height=792).insert_htmlbox(pymupdf.Rect(40, 40, 572, 752), cover, css=CSS)
    p2 = mixed.new_page(width=612, height=792)
    p2.insert_image(p2.rect, pixmap=sp.get_pixmap(dpi=110, colorspace=pymupdf.csGRAY))
    (DOCS / "co_10_mixed.pdf").write_bytes(mixed.tobytes(garbage=3, deflate=True))
    gold("co_10_mixed", [dict(
        change_order_number="31", project_name="Bayview Senior Living", project_number="BSL-77",
        date_issued="2026-10-02", contractor="Harbor Line Builders", owner="Bayview Care Group",
        reason="owner_request", status="approved", subtotal=21760.0, markup_amount=1740.8,
        tax_amount=None, total_amount=23500.8, schedule_impact_days=6, approved_by="Grace Okafor",
        references=[], n_line_items=2)])


if __name__ == "__main__":
    main()
