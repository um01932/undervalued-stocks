content = open("data/reports/full_report.html", encoding="utf-8").read()

idx = content.find('id="why-LULU"')
if idx >= 0:
    chunk = content[idx:idx+6000]
    # Find the viewBox SVG (52w chart)
    vb_start = chunk.find("viewBox")
    if vb_start >= 0:
        # back up to the <svg tag
        svg_start = chunk.rfind("<svg", 0, vb_start)
        out = chunk[svg_start:svg_start+1200]
        open("scripts/_debug_out.txt", "w", encoding="utf-8").write(out)
        print("Written viewBox SVG to scripts/_debug_out.txt")
    else:
        # show from 1000 onwards
        open("scripts/_debug_out.txt", "w", encoding="utf-8").write(chunk[1000:3000])
        print("No viewBox — wrote rest of chunk")
