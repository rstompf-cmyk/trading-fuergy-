# -*- coding: utf-8 -*-
"""workers/ — samostatné procesy mimo web requestu.

control_loop: real-time tick VPP flotily (izolovaný od webu).
(neskôr: compute_worker = job queue, scheduler = cron trigger, data_fetcher).
"""
