MULTI-TAP WATERGUARD — MAIN.CPP ALIGNED DASHBOARD

This dashboard is aligned to the current WaterGuard firmware serial output.

Features:
- Upload-only Wokwi log input
- Tap A/B/C potentiometer positions and CMD state
- V1+V2+V3 normalized voltage
- Expected/measured/deviation flow
- Common valve and alarm state
- Leak SIM and possible leak analysis
- Protection timer with 20s warning and 25s critical markers
- Auto-shutoff and reset event timeline
- Tap opening history
- Tap command utilization
- Estimated water usage
- CSV export

Run:
    py -m pip install -r requirements.txt
    py -m streamlit run app.py
