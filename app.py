import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import io
import xlsxwriter
from pandas.tseries.holiday import USFederalHolidayCalendar
from pandas.tseries.offsets import CustomBusinessDay

# --- 1. PAGE CONFIGURATION ---
st.set_page_config(
    page_title="STIR Trader Pro",
    page_icon="🏛️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- 2. CUSTOM CSS (GREEN MODE) ---
st.markdown("""
    <style>
    /* Main Background */
    .stApp { background-color: #0f172a; color: #10b981; }
    
    /* Inputs */
    .stTextInput > div > div > input, 
    .stNumberInput > div > div > input, 
    .stSelectbox > div > div > div {
        color: #10b981;
        background-color: #1e293b;
        border-color: #334155;
    }
    
    /* Headers & Labels */
    h1, h2, h3, h4, p, label, .stMarkdown, span {
        color: #10b981 !important;
    }
    
    /* Metrics */
    div[data-testid="stMetricValue"] { color: #10b981; }
    
    /* Dataframes */
    div[data-testid="stDataFrame"] { background-color: #1e293b; }
    
    /* Tabs */
    button[data-baseweb="tab"] { color: #10b981; }
    button[data-baseweb="tab"][aria-selected="true"] {
        background-color: #1e293b;
        border-bottom: 2px solid #10b981;
    }
    
    /* Buttons */
    .stButton > button {
        background-color: #10b981;
        color: #0f172a;
        font-weight: bold;
        border: none;
    }
    .stButton > button:hover {
        background-color: #34d399;
        color: #0f172a;
    }
    </style>
""", unsafe_allow_html=True)

# --- 3. LOGIC ENGINE ---
@st.cache_resource
def get_engine():
    return MarketDataEngine()

class MarketDataEngine:
    def __init__(self):
        self.fomc_schedule = {
            2024: ['2024-01-31', '2024-03-20', '2024-05-01', '2024-06-12', '2024-07-31', '2024-09-18', '2024-11-07', '2024-12-18'],
            2025: ['2025-01-29', '2025-03-19', '2025-05-07', '2025-06-18', '2025-07-30', '2025-09-17', '2025-10-29', '2025-12-10'],
            2026: ['2026-01-28', '2026-03-18', '2026-04-29', '2026-06-17', '2026-07-29', '2026-09-16', '2026-10-28', '2026-12-09'],
            2027: ['2027-01-27', '2027-03-17', '2027-04-28', '2027-06-09', '2027-07-28', '2027-09-15', '2027-10-27', '2027-12-08']
        }
        self.cal = USFederalHolidayCalendar()
        self.bd = CustomBusinessDay(calendar=self.cal)

    def get_effective_date(self, meeting_date_str):
        return pd.to_datetime(meeting_date_str) + self.bd

    def generate_impact_matrix(self, year):
        start, end = pd.Timestamp(f"{year}-01-01"), pd.Timestamp(f"{year}-12-31")
        meetings = self.fomc_schedule.get(year, [])
        matrix_data = []
        for m_str in meetings:
            eff_date = self.get_effective_date(m_str)
            row = {'Meeting': m_str, 'Effective': eff_date.strftime('%Y-%m-%d')}
            for month_idx in range(1, 13):
                m_start = pd.Timestamp(f"{year}-{month_idx:02d}-01")
                m_end = m_start + pd.offsets.MonthEnd(0)
                total_days = (m_end - m_start).days + 1
                overlap_start, overlap_end = max(eff_date, m_start), min(end, m_end)
                factor = max(0, (overlap_end - overlap_start).days + 1) / total_days if overlap_start <= overlap_end else 0.0
                row[m_start.strftime('%b')] = factor
            matrix_data.append(row)
        return pd.DataFrame(matrix_data)

    def generate_daily_curve(self, year, effr_start, sofr_start, hikes_map, turn_premiums):
        start_date, end_date = pd.Timestamp(f"{year}-01-01"), pd.Timestamp(f"{year}-12-31")
        dates = pd.date_range(start=start_date, end=end_date, freq='D')
        change_series = pd.Series(0.0, index=dates)
        
        for m_date, change in hikes_map.items():
            eff_date = self.get_effective_date(m_date)
            if start_date <= eff_date <= end_date:
                change_series.loc[eff_date] = change
                
        rate_path_effr = change_series.cumsum() + effr_start
        rate_path_sofr = change_series.cumsum() + sofr_start
        
        for d in dates:
            addon = 0.0
            if d.is_year_end: addon = turn_premiums['Year End']
            elif d.is_quarter_end: addon = turn_premiums['Quarter End']
            elif d.is_month_end: addon = turn_premiums['Month End']
            
            rate_path_effr.loc[d] += addon
            rate_path_sofr.loc[d] += addon
            
        df = pd.DataFrame({'Date': dates, 'EFFR': rate_path_effr.values, 'SOFR': rate_path_sofr.values})
        # DAY OF WEEK ADDED
        df['Day_Name'] = df['Date'].dt.day_name()
        return df[['Date', 'Day_Name', 'EFFR', 'SOFR']]

    def calculate_pricing(self, daily_df):
        daily_df['Month_ID'] = daily_df['Date'].dt.to_period('M')
        monthly = daily_df.groupby('Month_ID').agg(Avg_EFFR=('EFFR', 'mean'), Avg_SOFR=('SOFR', 'mean')).reset_index()
        monthly['ZQ'] = 100 - monthly['Avg_EFFR']
        monthly['SR1'] = 100 - monthly['Avg_SOFR']
        monthly['Basis_bps'] = (monthly['ZQ'] - monthly['SR1']) * 100
        monthly['Month_Label'] = monthly['Month_ID'].dt.strftime('%b %Y')
        codes = {1:'F', 2:'G', 3:'H', 4:'J', 5:'K', 6:'M', 7:'N', 8:'Q', 9:'U', 10:'V', 11:'X', 12:'Z'}
        monthly['Code'] = monthly['Month_ID'].dt.month.map(codes)
        return monthly

    def calculate_strategies(self, pricing):
        spreads = []
        for i in range(len(pricing)-1):
            s, e = pricing.iloc[i], pricing.iloc[i+1]
            spreads.append({'Strategy': f"{s['Code']}/{e['Code']}", 'Price': (s['SR1'] - e['SR1']) * 100, 'Type': 'Spread'})
        q_months = [3, 6, 9, 12]
        q_df = pricing[pricing['Month_ID'].dt.month.isin(q_months)].reset_index(drop=True)
        for i in range(len(q_df)-2):
            w1, body, w2 = q_df.iloc[i], q_df.iloc[i+1], q_df.iloc[i+2]
            spreads.append({'Strategy': f"{body['Code']} Fly", 'Price': (2*body['SR1'] - (w1['SR1'] + w2['SR1'])) * 100, 'Type': 'Fly'})
        return pd.DataFrame(spreads)

engine = get_engine()

if 'scenarios' not in st.session_state:
    st.session_state.scenarios = {}

# --- 4. SIDEBAR ---
st.sidebar.title("Global Parameters")
w_year = st.sidebar.selectbox("Year", [2024, 2025, 2026, 2027], index=2)
w_effr = st.sidebar.number_input("Start EFFR (%)", value=5.33, step=0.01)
w_sofr = st.sidebar.number_input("Start SOFR (%)", value=5.33, step=0.01)
st.sidebar.markdown("---")
tp_m = st.sidebar.number_input("Month End (bps)", value=0.0, step=1.0)
tp_q = st.sidebar.number_input("Quarter End (bps)", value=10.0, step=1.0)
tp_y = st.sidebar.number_input("Year End (bps)", value=25.0, step=1.0)

# --- 5. DASHBOARD ---
st.title("🏛️ STIR TRADER PRO")
st.markdown(f"**Curve: {w_year}**")

# Hikes
st.subheader("FOMC Adjustments (bps)")
dates = engine.fomc_schedule.get(w_year, [])
hikes = {}
cols = st.columns(4)
for i, d in enumerate(dates):
    label = pd.to_datetime(d).strftime('%b %d')
    with cols[i % 4]:
        val = st.number_input(f"{label}", value=0.0, step=25.0, key=f"meet_{d}")
        hikes[d] = val / 100.0

# Calculation
turns = {'Month End': tp_m/100, 'Quarter End': tp_q/100, 'Year End': tp_y/100}
daily = engine.generate_daily_curve(w_year, w_effr, w_sofr, hikes, turns)
pricing = engine.calculate_pricing(daily)
impact = engine.generate_impact_matrix(w_year)
strategies = engine.calculate_strategies(pricing)

# --- TABS ---
tab1, tab2, tab3 = st.tabs(["📊 Builder", "🛠️ Spread Maker & Analysis", "💾 Export"])

with tab1:
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=daily['Date'], y=daily['EFFR'], name='EFFR', line=dict(color='#10b981', width=2)))
    fig.add_trace(go.Scatter(x=daily['Date'], y=daily['SOFR'], name='SOFR', line=dict(color='#3b82f6', dash='dot')))
    fig.update_layout(height=450, paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', font_color='#10b981',
                      margin=dict(l=20, r=20, t=20, b=20), legend=dict(orientation="h", y=1.02))
    st.plotly_chart(fig, use_container_width=True)
    st.subheader("Pricing Strip")
    st.dataframe(pricing[['Month_Label', 'Code', 'ZQ', 'SR1', 'Basis_bps']].style.format({'ZQ':'{:.3f}', 'SR1':'{:.3f}', 'Basis_bps':'{:+.1f}'}).background_gradient(cmap='Greens', subset=['Basis_bps']), use_container_width=True)

with tab2:
    # --- CUSTOM SPREAD MAKER ---
    st.markdown("### 🛠️ Custom Spread Maker")
    with st.container():
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            leg1_type = st.selectbox("Leg 1", ["SR1", "ZQ"], key="l1t")
            leg1_m = st.selectbox("Month", pricing['Month_Label'], key="l1m")
        with c2:
            leg2_type = st.selectbox("Leg 2", ["SR1", "ZQ"], key="l2t")
            leg2_m = st.selectbox("Month", pricing['Month_Label'], key="l2m", index=1 if len(pricing)>1 else 0)
        
        # Calc Custom Spread
        p1 = pricing.loc[pricing['Month_Label'] == leg1_m, leg1_type].values[0]
        p2 = pricing.loc[pricing['Month_Label'] == leg2_m, leg2_type].values[0]
        custom_val = (p1 - p2) * 100
        
        with c3:
            st.metric("Spread Value (bps)", f"{custom_val:+.1f}")
        with c4:
            st.caption(f"{leg1_type} {leg1_m} vs {leg2_type} {leg2_m}")
            
    st.markdown("---")
    
    # --- BASIS ANALYZER & STRATEGIES ---
    col_l, col_r = st.columns(2)
    with col_l:
        st.subheader("📉 Basis Analyzer (ZQ - SR1)")
        # Chart for Basis
        fig_basis = go.Figure()
        fig_basis.add_trace(go.Bar(x=pricing['Code'], y=pricing['Basis_bps'], name='Basis', marker_color='#10b981'))
        fig_basis.update_layout(height=350, paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', font_color='#10b981', title="Basis Spread Term Structure")
        st.plotly_chart(fig_basis, use_container_width=True)

    with col_r:
        st.subheader("Standard Strategy Ladder")
        st.dataframe(strategies.style.format({'Price': '{:+.1f}'}).background_gradient(cmap='RdYlGn', subset=['Price']), use_container_width=True)

with tab3:
    st.subheader("Scenario Manager")
    c1, c2 = st.columns([3, 1])
    with c1: scenario_name = st.text_input("Scenario Name", value="Base Case")
    with c2: 
        st.write(""); st.write("")
        if st.button("Save Scenario"):
            st.session_state.scenarios[scenario_name] = pricing.copy()
            st.success(f"Saved: {scenario_name}")

    if st.session_state.scenarios:
        st.write("---")
        st.subheader("Export to Excel")
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            pricing.to_excel(writer, sheet_name='Active_Pricing', index=False)
            daily.to_excel(writer, sheet_name='Active_Daily_Rates', index=False) # Has Day_Name
            strategies.to_excel(writer, sheet_name='Active_Strategies', index=False)
            
            dfs = []
            for name, df in st.session_state.scenarios.items():
                temp = df[['Month_Label', 'SR1']].copy()
                temp.rename(columns={'SR1': name}, inplace=True)
                dfs.append(temp)
            if dfs:
                final_comp = dfs[0]
                for i in range(1, len(dfs)): final_comp = pd.merge(final_comp, dfs[i], on='Month_Label')
                final_comp.to_excel(writer, sheet_name='Scenario_Comparison', index=False)
                
            wb = writer.book
            ws = writer.sheets['Active_Pricing']
            chart = wb.add_chart({'type': 'line'})
            max_row = len(pricing) + 1
            chart.add_series({'name': 'SR1 Price', 'categories': ['Active_Pricing', 1, 0, max_row-1, 0], 'values': ['Active_Pricing', 1, 3, max_row-1, 3], 'line': {'color': '#10b981', 'width': 2.25}})
            ws.insert_chart('G2', chart)
                
        output.seek(0)
        st.download_button("📥 Download STIR_Master.xlsx", output, "STIR_Master.xlsx")
