"""
WindWalker NYC â Streamlit Web App
===================================
Find the wind-sheltered walking route between two NYC addresses,
using live weather + real building geometry + the canyon wind formula.

Run locally:
    pip install streamlit streamlit-folium folium requests
    streamlit run app.py

Deploy:
    Push to GitHub â connect on share.streamlit.io â one-click deploy
"""

import streamlit as st
import folium
from streamlit_folium import st_folium
import math, time as time_mod

# ââ page config (must be first Streamlit call) âââââââââââââââââââââââââââââââ
st.set_page_config(
    page_title="WindWalker NYC",
    page_icon="ð¬",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ââ import the engine âââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
try:
    from windwalker_core import run, fetch_hourly_wind, describe_wind
    ENGINE_OK = True
except ImportError as e:
    ENGINE_OK = False
    ENGINE_ERR = str(e)

# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# STYLES
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
st.markdown("""
<style>
  /* ââ global ââ */
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;900&display=swap');
  html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

  /* ââ hide Streamlit chrome ââ */
  #MainMenu, footer, header { visibility: hidden; }

  /* ââ hero header ââ */
  .hero {
    background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 60%, #0e4d6b 100%);
    border-radius: 16px;
    padding: 36px 40px 28px;
    margin-bottom: 24px;
    color: white;
  }
  .hero h1 { font-size: 2.6rem; font-weight: 900; margin: 0; letter-spacing: -0.5px; }
  .hero p  { font-size: 1.05rem; opacity: 0.75; margin: 6px 0 0; }

  /* ââ metric cards ââ */
  .metric-row { display: flex; gap: 12px; margin: 16px 0; }
  .metric-card {
    background: white;
    border: 1px solid #e2e8f0;
    border-radius: 12px;
    padding: 16px 20px;
    flex: 1;
    box-shadow: 0 1px 4px rgba(0,0,0,.06);
  }
  .metric-card .label { font-size: .75rem; color: #64748b; font-weight: 600; text-transform: uppercase; letter-spacing: .04em; }
  .metric-card .value { font-size: 1.6rem; font-weight: 800; color: #0f172a; line-height: 1.1; margin-top: 4px; }
  .metric-card .sub   { font-size: .8rem; color: #94a3b8; margin-top: 2px; }

  /* ââ route comparison ââ */
  .route-box {
    border-radius: 12px;
    padding: 18px 22px;
    margin: 8px 0;
    border: 2px solid transparent;
  }
  .route-shortest  { background: #eff6ff; border-color: #3b82f6; }
  .route-sheltered { background: #f0fdf4; border-color: #22c55e; }
  .route-box .route-title { font-weight: 700; font-size: 1rem; margin-bottom: 4px; }
  .route-box .route-stat  { font-size: .88rem; color: #475569; }

  /* ââ wind badge ââ */
  .wind-badge {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 8px 16px; border-radius: 999px;
    font-weight: 700; font-size: 1rem;
    margin-bottom: 8px;
  }

  /* ââ reduction pill ââ */
  .pill {
    display: inline-block;
    padding: 4px 14px;
    border-radius: 999px;
    font-size: .85rem;
    font-weight: 700;
  }
  .pill-green  { background: #dcfce7; color: #15803d; }
  .pill-yellow { background: #fef9c3; color: #854d0e; }
  .pill-grey   { background: #f1f5f9; color: #475569; }

  /* ââ sidebar / inputs ââ */
  .stTextInput input { border-radius: 8px !important; }
  .stButton button {
    width: 100%; border-radius: 10px !important;
    background: #2563eb !important; color: white !important;
    font-weight: 700 !important; font-size: 1rem !important;
    padding: 0.6rem !important;
    border: none !important;
  }
  .stButton button:hover { background: #1d4ed8 !important; }
</style>
""", unsafe_allow_html=True)


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# HELPERS
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

WIND_COLORS = {
    "calm":     "#4ade80",
    "light":    "#fbbf24",
    "moderate": "#fb923c",
    "strong":   "#f87171",
    "severe":   "#dc2626",
}

def route_color_for_score(score: float) -> str:
    if score < 5:  return "#4ade80"
    if score < 12: return "#fbbf24"
    if score < 20: return "#fb923c"
    return "#f87171"


def build_map(result: dict) -> folium.Map:
    """Render both routes + wind-scored street overlay on a Folium map."""
    mid = [(result["orig_ll"][0] + result["dest_ll"][0]) / 2,
           (result["orig_ll"][1] + result["dest_ll"][1]) / 2]
    m = folium.Map(location=mid, zoom_start=15, tiles="CartoDB positron")

    # ââ wind heat overlay (all streets, colour = wind score) âââââââââââââââââ
    for ed in result["edge_data"]:
        coords_ll = [(c[0], c[1]) for c in ed["coords"]]
        folium.PolyLine(
            coords_ll,
            color=route_color_for_score(ed["score"]),
            weight=3, opacity=0.5,
            tooltip=f"Wind score: {ed['score']:.1f} | Canyon: {ed['canyon']:.2f}",
        ).add_to(m)

    # ââ shortest route (blue, dashed) ââââââââââââââââââââââââââââââââââââââââ
    short_coords = [(c[0], c[1]) for c in result["short_s"]["coords"]]
    folium.PolyLine(
        short_coords,
        color="#2563eb", weight=5, opacity=0.9, dash_array="8 4",
        tooltip=f"Direct route: {result['short_s']['length_m']}m Â· {result['short_s']['length_min']} min",
    ).add_to(m)

    # ââ sheltered route (green, solid) âââââââââââââââââââââââââââââââââââââââ
    wind_coords = [(c[0], c[1]) for c in result["wind_s"]["coords"]]
    folium.PolyLine(
        wind_coords,
        color="#16a34a", weight=6, opacity=0.95,
        tooltip=f"Sheltered route: {result['wind_s']['length_m']}m Â· {result['wind_s']['length_min']} min",
    ).add_to(m)

    # ââ origin / destination markers âââââââââââââââââââââââââââââââââââââââââ
    folium.Marker(
        result["orig_ll"],
        popup="Start",
        icon=folium.Icon(color="blue", icon="play", prefix="fa"),
    ).add_to(m)
    folium.Marker(
        result["dest_ll"],
        popup="Destination",
        icon=folium.Icon(color="green", icon="flag", prefix="fa"),
    ).add_to(m)

    # ââ legend âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    legend_html = """
    <div style="position:fixed;bottom:20px;right:20px;z-index:1000;
                background:white;border-radius:10px;padding:12px 16px;
                box-shadow:0 2px 8px rgba(0,0,0,0.15);font-size:13px;font-family:Inter,sans-serif;">
      <div style="font-weight:700;margin-bottom:8px;">Routes</div>
      <div><span style="display:inline-block;width:28px;height:3px;
           background:#2563eb;border-top:2px dashed #2563eb;margin-right:6px;"></span>Direct</div>
      <div style="margin-top:4px"><span style="display:inline-block;width:28px;height:3px;
           background:#16a34a;margin-right:6px;vertical-align:middle;"></span>Sheltered</div>
      <div style="font-weight:700;margin:8px 0 4px;">Wind exposure</div>
      <div>ð¢ Low &nbsp; ð¡ Moderate &nbsp; ð  High &nbsp; ð´ Very high</div>
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend_html))
    return m


def reduction_pill(pct: float) -> str:
    if pct >= 15:
        cls = "pill-green"
        txt = f"â {pct:.1f}% less wind"
    elif pct >= 5:
        cls = "pill-yellow"
        txt = f"â {pct:.1f}% less wind"
    else:
        cls = "pill-grey"
        txt = "Similar wind exposure"
    return f'<span class="pill {cls}">{txt}</span>'


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# MAIN UI
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

# ââ hero âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
st.markdown("""
<div class="hero">
  <h1>ð¬ WindWalker NYC</h1>
  <p>Find the wind-sheltered walking route â live weather Â· real building heights Â· urban canyon formula</p>
</div>
""", unsafe_allow_html=True)

if not ENGINE_OK:
    st.error(f"â  Engine import failed: `{ENGINE_ERR}`. Make sure `windwalker_core.py` is in the same folder.")
    st.stop()

# ââ input columns âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
col_form, col_map = st.columns([1, 2], gap="large")

with col_form:
    st.markdown("#### ð Route")
    origin = st.text_input("From", value="",
                            placeholder="e.g. Penn Station, New York, NY")
    dest   = st.text_input("To",   value="",
                            placeholder="e.g. Grand Central Terminal, New York, NY")

    # ââ time picker ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    st.markdown("#### â° When are you walking?")
    @st.cache_data(ttl=1800)
    def get_forecast():
        try:
            # centre of Manhattan as reference
            return fetch_hourly_wind(40.7549, -73.9840)
        except Exception as e:
            return None

    forecast_data = get_forecast()
    if forecast_data:
        hours = forecast_data["forecast"][:13]   # now + 12h
        labels = [f"{h['label']}  ({h['speed']} mph {describe_wind(h['speed'], h['direction'])['compass']})"
                  for h in hours]
        hour_idx = st.selectbox("Select hour", range(len(labels)),
                                format_func=lambda i: labels[i])
    else:
        hour_idx = 0
        st.caption("Could not load forecast â will use current conditions.")

    # ââ run button âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    run_btn = st.button("ð Find Wind-Sheltered Route", use_container_width=True,
                        disabled=(not origin.strip() or not dest.strip()))
    if run_btn and (not origin.strip() or not dest.strip()):
        st.warning("Please enter both a start and end address.")

    # ââ result panel (below button, in form column) âââââââââââââââââââââââââââ
    if "result" in st.session_state and st.session_state.result:
        res = st.session_state.result
        w   = res["wind"]

        st.markdown("---")
        st.markdown("#### ð¬ Wind conditions")
        badge_color = WIND_COLORS.get(w["severity"], "#94a3b8")
        st.markdown(
            f'<div class="wind-badge" style="background:{badge_color}20;color:{badge_color};">'
            f'{w["emoji"]} {w["description"]} â <em>{w["severity"]}</em></div>',
            unsafe_allow_html=True,
        )
        st.caption(f"Gusts up to {res['wind_gusts']} mph Â· {res['wind_time']}")

        st.markdown("#### ðº Route comparison")
        st.markdown(f"""
        <div class="route-box route-shortest">
          <div class="route-title">ðµ Direct route</div>
          <div class="route-stat">{res['short_s']['length_m']}m &nbsp;Â·&nbsp; ~{res['short_s']['length_min']} min
          &nbsp;Â·&nbsp; wind score {res['short_s']['avg_wind_score']}</div>
        </div>
        <div class="route-box route-sheltered">
          <div class="route-title">ð¢ Sheltered route &nbsp; {reduction_pill(res['reduction'])}</div>
          <div class="route-stat">{res['wind_s']['length_m']}m &nbsp;Â·&nbsp; ~{res['wind_s']['length_min']} min
          &nbsp;Â·&nbsp; wind score {res['wind_s']['avg_wind_score']}</div>
        </div>
        """, unsafe_allow_html=True)

        if res["same_route"]:
            st.info("Both routes are identical at this wind level.")

        with st.expander("ð Details"):
            st.write(f"**Buildings loaded:** {res['n_buildings']:,} "
                     f"({'PLUTO â' if res['used_pluto'] else 'OSM fallback'})")
            st.write(f"**Street edges scored:** {res['n_edges']:,}")
            st.write(f"**Wind direction:** {w['direction']}Â° ({w['compass']})")
            st.write(f"**Canyon tunnel risk:** {'Yes' if w['tunnel_risk'] else 'Low'}")

# ââ map column ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
with col_map:
    if run_btn:
        with st.spinner("ð Fetching wind, buildings & streetsâ¦"):
            prog_bar = st.progress(0)
            status   = st.empty()

            def on_progress(pct, msg):
                prog_bar.progress(pct)
                status.caption(msg)

            try:
                result = run(
                    origin, dest,
                    forecast_hour_index=hour_idx,
                    progress_cb=on_progress,
                )
                st.session_state.result = result
                prog_bar.empty()
                status.empty()
            except Exception as e:
                prog_bar.empty()
                status.empty()
                st.error(f"â {e}")
                st.session_state.result = None

    if "result" in st.session_state and st.session_state.result:
        fmap = build_map(st.session_state.result)
        st_folium(fmap, width=None, height=540, returned_objects=[])
    else:
        # placeholder map â Manhattan
        m0 = folium.Map(location=[40.754, -73.984], zoom_start=14,
                        tiles="CartoDB positron")
        st_folium(m0, width=None, height=540, returned_objects=[])
        st.caption("Enter addresses above and click **Find Wind-Sheltered Route**.")

# ââ footer ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
st.markdown("---")
st.markdown(
    '<div style="text-align:center;color:#94a3b8;font-size:.8rem;">'
    'ð¬ WindWalker NYC &nbsp;Â·&nbsp; '
    'Formula: <code>wind_score = wind_speed Ã |cos(bearing â wind_dir)| Ã canyon_factor</code> &nbsp;Â·&nbsp; '
    'Data: Open-Meteo Â· OpenStreetMap Â· NYC PLUTO'
    '</div>',
    unsafe_allow_html=True,
)
