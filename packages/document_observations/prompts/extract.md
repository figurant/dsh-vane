Extract operational observations grounded in the supplied document or image.
Return JSON only: {"observations":[{"item":"A","value":10,"category":null,"quote":"verbatim supporting text","page":1}]}.
Use null for unavailable value/category. Do not invent facts or locations.
page is one-based for PDFs only. Images may have a normalized bbox [x0,y0,x1,y1].
No bbox or page is better than an invented location. Retain conflicting observations.
