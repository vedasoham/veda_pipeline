import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import json
from pathlib import Path
import sys

# --- Add src to path to import config ---
SRC_DIR = Path(__file__).parent.parent.resolve()
if str(SRC_DIR) not in sys.path:
    sys.path.append(str(SRC_DIR))

from config import RESULTS_DIR, PROJECT_METADATA_FILENAME

# =============================================================================
# 1. STATE MANAGER
# =============================================================================
class StateManager:
    """
    Checks for the presence of output files and metadata to determine which 
    tools were run and loads their data safely.
    """
    def __init__(self, project_name: str):
        self.project_dir = RESULTS_DIR / project_name
        self.meta_file = self.project_dir / PROJECT_METADATA_FILENAME
        self.metadata = self._load_metadata()

    def _load_metadata(self):
        if self.meta_file.exists():
            with open(self.meta_file, 'r') as f:
                return json.load(f)
        return {}

    def is_tool_run(self, tool_name: str, expected_file: str) -> bool:
        file_path = self.project_dir / expected_file
        return file_path.exists()

    def load_data(self, expected_file: str) -> pd.DataFrame:
        file_path = self.project_dir / expected_file
        if not file_path.exists():
            return None
        
        # Naive loader: assumes CSV/TSV. Adjust based on your actual output formats.
        try:
            if expected_file.endswith('.csv'):
                return pd.read_csv(file_path)
            elif expected_file.endswith('.tsv') or expected_file.endswith('.txt'):
                return pd.read_csv(file_path, sep='\t')
            elif expected_file.endswith('.json'):
                return pd.read_json(file_path)
        except Exception as e:
            st.error(f"Error loading {expected_file}: {e}")
            return None
        return None

# =============================================================================
# DUMMY DATA GENERATOR (For Demonstration Purposes)
# Remove this in production and replace with actual `state.load_data()` calls
# =============================================================================
def get_mock_data():
    """Generates mock data to demonstrate the Plotly visualizations."""
    import numpy as np
    import random
    
    # Heatmap Data (MHC)
    alleles = ["HLA-A*02:01", "HLA-B*07:02", "HLA-DRB1*01:01"]
    peptides = [f"PEP_{i}" for i in range(1, 11)]
    heatmap_df = pd.DataFrame(np.random.uniform(0.1, 5.0, size=(10, 3)), columns=alleles, index=peptides)
    
    # Sunburst Data
    sunburst_df = pd.DataFrame({
        'Protein': ['Prot_A', 'Prot_A', 'Prot_B', 'Prot_C'],
        'Localization': ['Extracellular', 'Transmembrane', 'Extracellular', 'Intracellular'],
        'Peptide': ['PEP_1', 'PEP_2', 'PEP_3', 'PEP_4'],
        'Count': [1, 1, 1, 1]
    })
    
    # Violin Data
    violin_df = pd.DataFrame({
        'Peptide': peptides,
        'UniProt_ID': [f"P{random.randint(10000, 99999)}" for _ in range(10)],
        'Protegenicity': np.random.normal(0.6, 0.2, 10)
    })
    
    # Safety Matrix Data
    safety_df = pd.DataFrame({
        'Peptide': peptides,
        'ToxinPred': random.choices(['Safe', 'Toxic'], k=10),
        'AlgPred': random.choices(['Safe', 'Allergen'], k=10),
        'IFN_gamma': random.choices(['Inducer', 'Non-Inducer'], k=10),
        'IL_4': random.choices(['Inducer', 'Non-Inducer'], k=10)
    })
    
    # Radar Data
    radar_df = pd.DataFrame({
        'Feature': ['Binding', 'Protegenicity', 'IFN-y', 'Safety', 'Non-Allergen'],
        'PEP_1': [0.9, 0.8, 0.7, 1.0, 1.0],
        'PEP_2': [0.6, 0.9, 0.4, 0.0, 1.0]
    })
    
    return heatmap_df, sunburst_df, violin_df, safety_df, radar_df

# =============================================================================
# 2. DASHBOARD UI & VISUALIZATIONS
# =============================================================================
st.set_page_config(page_title="IEL Pipeline Dashboard", layout="wide")

st.title("🧬 IEL Pipeline: Immunoinformatics Dashboard")

# --- Project Selection ---
# In reality, you'd scan RESULTS_DIR for available projects.
project_name = st.selectbox("Select Project Run", ["Sample_Run_001", "Sample_Run_002"])
state = StateManager(project_name)

# Let's assume we grabbed counts from the state manager. Mocking for now:
counts = {
    "Input": 5000,
    "MHC": 1200,
    "Vaxign": 400,
    "Safety": 150,
    "Cytokine": 45
}

# --- 1. The Statistical "Funnel" Header ---
st.markdown("### 📊 Pipeline Attrition Summary")
cols = st.columns(5)

cols[0].metric("Input Peptides", counts["Input"])

if state.is_tool_run("NetMHC", "netmhc_results.tsv") or True: # Mocked True
    cols[1].metric("MHC Binders", counts["MHC"], f"{(counts['MHC']/counts['Input'])*100:.1f}%")
else:
    cols[1].metric("MHC Binders", "N/A")

if state.is_tool_run("Vaxign", "vaxign_results.tsv") or True:
    cols[2].metric("Protective Antigens (Vaxign)", counts["Vaxign"], f"{(counts['Vaxign']/counts['MHC'])*100:.1f}%")
else:
    cols[2].metric("Protective Antigens (Vaxign)", "N/A")

if state.is_tool_run("ToxinPred", "toxin_results.tsv") or True:
    cols[3].metric("Safe & Non-Allergenic", counts["Safety"], f"{(counts['Safety']/counts['Vaxign'])*100:.1f}%")
else:
    cols[3].metric("Safe & Non-Allergenic", "N/A")

if state.is_tool_run("IFNEpitope", "ifn_results.tsv") or True:
    cols[4].metric("Target Cytokine Inducers", counts["Cytokine"], f"{(counts['Cytokine']/counts['Safety'])*100:.1f}%")
else:
    cols[4].metric("Target Cytokine Inducers", "N/A")

st.divider()

# Fetching Dummy Data for UI construction
hm_df, sb_df, v_df, sf_df, rd_df = get_mock_data()

# --- 2. Interactive Graphs ---
st.markdown("### 📈 Interactive Analytics")

tab1, tab2, tab3 = st.tabs(["Overview & Binding", "Protegenicity & Structure", "Safety & Final Candidates"])

with tab1:
    colA, colB = st.columns(2)
    
    with colA:
        st.markdown("#### A. Pipeline Attrition Flow")
        # Sankey Diagram
        fig_sankey = go.Figure(data=[go.Sankey(
            node=dict(
                pad=15, thickness=20,
                line=dict(color="black", width=0.5),
                label=["Input Peptides", "MHC Binders", "Vaxign Winners", "Safe Epitopes", "Cytokine Positive"],
                color=["#A0C4FF", "#7DAFF9", "#5A9AF2", "#3685EC", "#1370E5"]
            ),
            link=dict(
                source=[0, 1, 2, 3], # Indices correspond to labels
                target=[1, 2, 3, 4],
                value=[counts["MHC"], counts["Vaxign"], counts["Safety"], counts["Cytokine"]]
            )
        )])
        fig_sankey.update_layout(margin=dict(l=0, r=0, t=30, b=0), height=400)
        st.plotly_chart(fig_sankey, use_container_width=True)

    with colB:
        st.markdown("#### B. MHC Binding Landscape")
        if state.is_tool_run("NetMHC", "netmhc.tsv") or True:
            # Heatmap
            fig_heat = px.imshow(
                hm_df, 
                labels=dict(x="HLA Alleles", y="Peptides", color="% Rank / Affinity"),
                color_continuous_scale="Viridis_r",
                aspect="auto"
            )
            fig_heat.update_layout(margin=dict(l=0, r=0, t=30, b=0), height=400)
            st.plotly_chart(fig_heat, use_container_width=True)
        else:
            st.info("ℹ️ MHC Prediction was skipped for this run.")

with tab2:
    colC, colD = st.columns(2)
    
    with colC:
        st.markdown("#### C. Topology & Localization Profile")
        if state.is_tool_run("DeepTMHMM", "tmhmm.tsv") or True:
            fig_sun = px.sunburst(
                sb_df, 
                path=['Protein', 'Localization', 'Peptide'], 
                values='Count',
                color='Localization',
                color_discrete_map={'Extracellular':'#2ca02c', 'Transmembrane':'#ff7f0e', 'Intracellular':'#d62728'}
            )
            fig_sun.update_layout(margin=dict(l=0, r=0, t=30, b=0), height=400)
            st.plotly_chart(fig_sun, use_container_width=True)
        else:
            st.info("ℹ️ DeepTMHMM was skipped for this run.")
            
    with colD:
        st.markdown("#### D. Protegenicity Distribution")
        if state.is_tool_run("Vaxign", "vaxign.tsv") or True:
            fig_vio = px.violin(
                v_df, y="Protegenicity", 
                box=True, points="all", 
                hover_data=["Peptide", "UniProt_ID"],
                color_discrete_sequence=['#9467bd']
            )
            # Add a threshold line
            fig_vio.add_hline(y=0.5, line_dash="dash", line_color="red", annotation_text="Threshold")
            fig_vio.update_layout(margin=dict(l=0, r=0, t=30, b=0), height=400)
            st.plotly_chart(fig_vio, use_container_width=True)
        else:
            st.info("ℹ️ Vaxign-ML was skipped for this run.")

with tab3:
    colE, colF = st.columns(2)
    
    with colE:
        st.markdown("#### E. Safety & Cytokine Matrix")
        if state.is_tool_run("ToxinPred", "toxin.tsv") or True:
            # Convert categorical text to numeric for plotting (0=Bad, 1=Good)
            num_df = sf_df.set_index('Peptide').replace(
                {'Safe': 1, 'Toxic': 0, 'Allergen': 0, 'Inducer': 1, 'Non-Inducer': 0}
            )
            
            fig_safe = px.imshow(
                num_df,
                labels=dict(x="Assay", y="Peptide", color="Status"),
                color_continuous_scale=[[0, "red"], [1, "green"]],
                aspect="auto"
            )
            fig_safe.update_coloraxes(showscale=False) # Hide scale, it's binary
            fig_safe.update_layout(margin=dict(l=0, r=0, t=30, b=0), height=400)
            st.plotly_chart(fig_safe, use_container_width=True)
        else:
            st.info("ℹ️ Safety or Cytokine predictive tools were skipped.")

    with colF:
        st.markdown("#### F. The 'Golden Candidate' Radar")
        fig_radar = go.Figure()
        
        for col in rd_df.columns[1:]:
            fig_radar.add_trace(go.Scatterpolar(
                r=rd_df[col].tolist(),
                theta=rd_df['Feature'].tolist(),
                fill='toself',
                name=col
            ))
            
        fig_radar.update_layout(
            polar=dict(radialaxis=dict(visible=True, range=[0, 1])),
            showlegend=True,
            margin=dict(l=40, r=40, t=30, b=0),
            height=400
        )
        st.plotly_chart(fig_radar, use_container_width=True)