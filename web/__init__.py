# -*- coding: utf-8 -*-
"""web/ — HTML routery (rozbíjanie app.py monolitu po doménach).

Prvý extrahovaný router: web.fleet (VPP fleet admin/monitor). Registruje sa v
app.py cez `app.include_router(web.fleet.router)`.
"""
from .fleet import router as fleet_router

__all__ = ["fleet_router"]
