import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import json
from pathlib import Path
import sys
import csv
import io
import numpy as np
import matplotlib.pyplot as plt
from matplotlib_venn import venn2, venn3
import subprocess

# --- Add src to path to import config ---
SRC_DIR = Path(__file__).parent.parent.resolve()
if str(SRC_DIR) not in sys.path:
    sys.path.append(str(SRC_DIR))

from config import DATA_DIR, PROJECT_METADATA_FILENAME

# =============================================================================
# PUBLICATION CONFIGURATION
# =============================================================================
PUB_CONFIG = {
    'displayModeBar': True,
    'displaylogo': False,
    'modeBarButtonsToRemove': ['zoom', 'pan', 'select', 'lasso2d'],
    'toImageButtonOptions': {
        'format': 'png',
        'filename': 'publication_figure',
        'height': 600,
        'width': 800,
        'scale': 3  # Multiplies resolution for print quality (300+ DPI)
    }
}
PUB_THEME = "simple_white"
PUB_FONT = dict(family="Arial", size=14, color="black")

# DPI for high-resolution export
EXPORT_DPI = 600

def color_to_hex(color):
    """Convert rgb/rgba strings to hex format expected by Streamlit's color picker."""
    if isinstance(color, str) and color.startswith('rgb'):
        import re
        nums = re.findall(r'\d+', color)
        if len(nums) >= 3:
            return f"#{int(nums[0]):02x}{int(nums[1]):02x}{int(nums[2]):02x}"
    return color

def render_figure_customization(key_prefix, default_title="", default_xtitle="X Axis", default_ytitle="Y Axis"):
    """Render customization controls for a figure."""
    with st.expander(f"Customize {key_prefix.upper()} Figure"):
        c1, c2 = st.columns(2)
        title = c1.text_input(f"Title ({key_prefix})", default_title, key=f"{key_prefix}_title")
        x_title = c2.text_input(f"X-axis Title ({key_prefix})", default_xtitle, key=f"{key_prefix}_xtitle")
        
        c3, c4 = st.columns(2)
        y_title = c3.text_input(f"Y-axis Title ({key_prefix})", default_ytitle, key=f"{key_prefix}_ytitle")
        font_size = c4.slider(f"Font Size ({key_prefix})", 8, 24, 14, key=f"{key_prefix}_fs")
        
        c5, c6 = st.columns(2)
        width = c5.number_input(f"Width (px) ({key_prefix})", 400, 1200, 800, key=f"{key_prefix}_w")
        height = c6.number_input(f"Height (px) ({key_prefix})", 300, 900, 600, key=f"{key_prefix}_h")
        
        # Checkboxes for options
        c7, c8 = st.columns(2)
        show_x_grid = c7.checkbox("X-axis inner grey lines", True, key=f"{key_prefix}_xgrid")
        show_y_grid = c8.checkbox("Y-axis inner grey lines", True, key=f"{key_prefix}_ygrid")
        
        c9, c10 = st.columns(2)
        show_labels = c9.checkbox("Count labels (in-bar)", True, key=f"{key_prefix}_labels")
        show_legend = c10.checkbox("Show legend", True, key=f"{key_prefix}_legend")
        
        return {
            'title': title, 'x_title': x_title, 'y_title': y_title, 'font_size': font_size,
            'width': width, 'height': height, 'show_x_grid': show_x_grid, 'show_y_grid': show_y_grid,
            'show_labels': show_labels, 'show_legend': show_legend
        }

# =============================================================================
# 1. STATE MANAGER
# =============================================================================
class StateManager:
    """
    Checks for the presence of output files and metadata to determine which 
    tools were run and loads their data safely.
    """
    def __init__(self, project_dir: Path):
        self.project_dir = project_dir
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

    def get_pipeline_counts(self):
        """Dynamically calculates real sequence counts from the pipeline output files."""
        counts = {"Input": "N/A", "MHC": "N/A", "Vaxign": "N/A", "Safety": "N/A", "Cytokine": "N/A"}
        
        def count_fasta(filename):
            path = self.project_dir / filename
            if path.exists():
                with open(path, 'r') as f:
                    return sum(1 for line in f if line.startswith(">"))
            return "N/A"

        # 1. Input Peptides (Check metadata first, fallback to FASTA)
        meta_uf = self.metadata.get("upload_and_filter", {}).get("outputs", {})
        if "n_peptides" in meta_uf and meta_uf["n_peptides"]:
            counts["Input"] = meta_uf["n_peptides"]
        else:
            counts["Input"] = count_fasta("peptide.fasta") or count_fasta("inputs/peptide.fasta")

        # 2. MHC Binders (Generated in Step 5 from Step 2/3 outputs)
        counts["MHC"] = count_fasta("inputs/SB_peptides.fasta")

        # 3. Vaxign Winners (Protective Antigens generated in Step 7)
        counts["Vaxign"] = count_fasta("inputs/vaxign_peptides.fasta")

        # 4. Safe Epitopes (Generated in Step 9)
        counts["Safety"] = count_fasta("inputs/safe_peptides.fasta")

        # 5. Cytokine Inducers (Combine IL-2 and IFN-g inducer IDs)
        il2_res = self.project_dir / "il2pred" / "il2pred" / "il2pred_results.csv"
        ifn_res = self.project_dir / "ifnepitope" / "ifnepitope" / "ifnepitope_results.csv"
        
        cytokine_set = set()
        for res_file, target_pred in [(il2_res, 'il2 inducing'), (ifn_res, 'IFN-γ inducer')]:
            if res_file.exists():
                try:
                    with open(res_file, 'r') as f:
                        for row in csv.DictReader(f):
                            pred = row.get('Prediction', '').strip()
                            if pred.lower() == target_pred.lower() or pred == target_pred:
                                cytokine_set.add(row.get('ID', row.get('Subject', '')))
                except Exception: pass
        
        if cytokine_set: counts["Cytokine"] = len(cytokine_set)
             
        return counts

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

    def get_real_master_df(self):
        """Constructs the master dataframe from real pipeline outputs and sorts from best to worst."""
        data = {}
        
        # Build header-to-sequence mapping to cross-reference tool outputs safely
        header_to_seq = {}
        for fasta_name in ["inputs/SB_peptides.fasta", "inputs/WB_peptides.fasta", "inputs/vaxign_peptides.fasta", "inputs/safe_peptides.fasta"]:
            fasta_path = self.project_dir / fasta_name
            if fasta_path.exists():
                try:
                    with open(fasta_path, 'r') as f:
                        curr_h = None
                        for line in f:
                            line = line.strip()
                            if line.startswith(">"):
                                curr_h = line[1:]
                            elif curr_h:
                                header_to_seq[curr_h] = line
                                header_to_seq[curr_h.split()[0]] = line # Fallback to first word
                                curr_h = None
                except Exception: pass

        # 1. Parse NetMHCpan / NetMHCIIpan
        for mhc_dir in ["netmhcpan", "netmhciipan"]:
            mhc_file = self.project_dir / mhc_dir / "netmhc_results.json"
            if mhc_file.exists():
                try:
                    with open(mhc_file, 'r') as f:
                        mhc_json = json.load(f)
                        for row in mhc_json:
                            pep = row.get('peptide')
                            if not pep: continue
                            try:
                                rank = float(row.get('rank', 99.9))
                            except ValueError:
                                rank = 99.9
                            allele = row.get('allele', 'Unknown')
                            
                            if pep not in data:
                                data[pep] = {
                                    'Peptide': pep, 'Binding_Rank': rank, 'Target_Allele': allele,
                                    'Protegenicity': 0.0, 'Safety_Status': 'Unknown', 'IFN_gamma_Score': 0.0
                                }
                            elif rank < data[pep]['Binding_Rank']:
                                data[pep]['Binding_Rank'] = rank
                                data[pep]['Target_Allele'] = allele
                except Exception: pass

        # 2. Safety (ToxinPred & AlgPred)
        toxin_file = self.project_dir / "toxinpred" / "toxinpred" / "toxinpred_results.csv"
        alg_file = self.project_dir / "algpred" / "algpred" / "algpred_results.csv"
        
        toxins = set()
        allergens = set()
        
        if toxin_file.exists():
            try:
                with open(toxin_file, 'r') as f:
                    for row in csv.DictReader(f):
                        pid = row.get('ID', row.get('Subject', '')).strip()
                        pep = header_to_seq.get(pid, pid)
                        if row.get('Prediction') != 'Non-Toxin':
                            toxins.add(pep)
            except Exception: pass
            
        if alg_file.exists():
            try:
                with open(alg_file, 'r') as f:
                    for row in csv.DictReader(f):
                        pid = row.get('Subject', row.get('ID', '')).strip()
                        pep = header_to_seq.get(pid, pid)
                        if row.get('Prediction') != 'Non-Allergen':
                            allergens.add(pep)
            except Exception: pass

        for pep in data:
            if pep in toxins or pep in allergens:
                data[pep]['Safety_Status'] = 'Toxic'
            elif toxin_file.exists() or alg_file.exists():
                data[pep]['Safety_Status'] = 'Safe'

        # 3. IFNEpitope (Cytokine)
        ifn_file = self.project_dir / "ifnepitope" / "ifnepitope" / "ifnepitope_results.csv"
        if ifn_file.exists():
            try:
                with open(ifn_file, 'r') as f:
                    for row in csv.DictReader(f):
                        pid = row.get('ID', row.get('Subject', '')).strip()
                        pep = header_to_seq.get(pid, pid)
                        pred = row.get('Prediction', '')
                        score = 1.0 if 'inducer' in pred.lower() else 0.0
                        if pep in data:
                            data[pep]['IFN_gamma_Score'] = score
            except Exception: pass

        # 4. Vaxign Protegenicity
        vax_peps = self.project_dir / "inputs/vaxign_peptides.fasta"
        if vax_peps.exists():
            try:
                with open(vax_peps, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if not line.startswith(">"):
                            if line in data:
                                data[line]['Protegenicity'] = 1.0
            except Exception: pass

        if not data:
            return pd.DataFrame()
            
        df = pd.DataFrame(list(data.values()))
        
        # Sort from best to worst
        df['Safety_Sort'] = df['Safety_Status'].apply(lambda x: 0 if x == 'Safe' else (1 if x == 'Unknown' else 2))
        df = df.sort_values(by=['Safety_Sort', 'Binding_Rank', 'Protegenicity', 'IFN_gamma_Score'], ascending=[True, True, False, False])
        df = df.drop(columns=['Safety_Sort'])
        
        return df

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
    
    # Master Data for Scatter & Table
    master_df = pd.DataFrame({
        'Peptide': peptides,
        'Binding_Rank': np.random.uniform(0.1, 2.0, 10),
        'Protegenicity': np.random.uniform(0.2, 0.9, 10),
        'Safety_Status': random.choices(['Safe', 'Toxic'], k=10),
        'Target_Allele': random.choices(alleles, k=10),
        'IFN_gamma_Score': np.random.uniform(0.0, 1.0, 10)
    })
    
    return heatmap_df, sunburst_df, violin_df, safety_df, master_df

# =============================================================================
# 2. DASHBOARD UI & VISUALIZATIONS
# =============================================================================
def render_dashboard():
    st.title("🧬 IEL Pipeline: Immunoinformatics Dashboard")

    # Connect to the live pipeline output directory
    state = StateManager(DATA_DIR)
    counts = state.get_pipeline_counts()

    def safe_pct(part, total):
        if isinstance(part, int) and isinstance(total, int) and total > 0:
            return f"{(part/total)*100:.1f}%"
        return "N/A"

    # --- 1. The Statistical "Funnel" Header ---
    st.markdown("### 📊 Pipeline Attrition Summary")
    cols = st.columns(5)

    cols[0].metric("Input Peptides", counts["Input"])

    if counts["MHC"] != "N/A":
        cols[1].metric("MHC Binders (SB)", counts["MHC"], safe_pct(counts["MHC"], counts["Input"]))
    else:
        cols[1].metric("MHC Binders", "N/A")

    if counts["Vaxign"] != "N/A":
        cols[2].metric("Protective Antigens", counts["Vaxign"], safe_pct(counts["Vaxign"], counts["MHC"]))
    else:
        cols[2].metric("Protective Antigens", "N/A")

    if counts["Safety"] != "N/A":
        cols[3].metric("Safe & Non-Allergenic", counts["Safety"], safe_pct(counts["Safety"], counts["Vaxign"]))
    else:
        cols[3].metric("Safe & Non-Allergenic", "N/A")

    if counts["Cytokine"] != "N/A":
        cols[4].metric("Cytokine Inducers", counts["Cytokine"], safe_pct(counts["Cytokine"], counts["Safety"]))
    else:
        cols[4].metric("Cytokine Inducers", "N/A")

    st.divider()

    # Fetching Dummy Data for UI construction
    hm_df, sb_df, v_df, sf_df, _ = get_mock_data()
    
    # Load real data for the master dataframe
    master_df = state.get_real_master_df()
    if master_df.empty:
        _, _, _, _, master_df = get_mock_data()
        st.info("ℹ️ Showing mock data for the Candidate Matrix because real pipeline results are not yet available.")

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
                    source=[0, 1, 2, 3],
                    target=[1, 2, 3, 4],
                    value=[
                        counts["MHC"] if counts["MHC"] != "N/A" else 0, 
                        counts["Vaxign"] if counts["Vaxign"] != "N/A" else 0, 
                        counts["Safety"] if counts["Safety"] != "N/A" else 0, 
                        counts["Cytokine"] if counts["Cytokine"] != "N/A" else 0
                    ]
                )
            )])
            fig_sankey.update_layout(margin=dict(l=0, r=0, t=30, b=0), height=400)
            if counts["MHC"] != "N/A": # Only show funnel if downstream steps ran
                st.plotly_chart(fig_sankey, use_container_width=True)

        with colB:
            st.markdown("#### B. MHC Binding Landscape")
            if counts["MHC"] != "N/A": # Just keeping mock visualization linked to completion state for now
                # Mock Data Placeholder
                fig_heat = px.imshow(
                    hm_df, 
                    labels=dict(x="HLA Alleles", y="Peptides", color="% Rank / Affinity"),
                    color_continuous_scale="Viridis_r",
                    aspect="auto"
                )
                fig_heat.update_layout(margin=dict(l=0, r=0, t=30, b=0), height=max(400, len(hm_df) * 25))
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
                fig_safe.update_layout(margin=dict(l=0, r=0, t=30, b=0), height=max(400, len(num_df) * 25))
                st.plotly_chart(fig_safe, use_container_width=True)
            else:
                st.info("ℹ️ Safety or Cytokine predictive tools were skipped.")

        with colF:
            st.markdown("#### F. The Candidate Matrix (All Results)")
            
            # X = Binding Rank, Y = Protegenicity, Color = Safety, Size = Cytokine, Hover = Peptide
            fig_scatter = px.scatter(
                master_df,
                x="Binding_Rank", 
                y="Protegenicity",
                color="Safety_Status",
                color_discrete_map={"Safe": "#2ca02c", "Toxic": "#d62728"},
                size="IFN_gamma_Score",
                hover_name="Peptide",
                hover_data=["Target_Allele", "IFN_gamma_Score"],
                title="Top Right Quadrant = Optimal Candidates"
            )
            
            # Invert X-axis since a lower rank (e.g., 0.1%) is a stronger binder
            fig_scatter.update_xaxes(autorange="reversed")
            fig_scatter.update_layout(height=400, margin=dict(l=0, r=0, t=40, b=0))
            
            # Add strict threshold lines
            fig_scatter.add_hline(y=0.5, line_dash="dot", annotation_text="Antigenicity Threshold")
            fig_scatter.add_vline(x=0.5, line_dash="dot", annotation_text="Strong Binder Threshold")
            
            st.plotly_chart(fig_scatter, use_container_width=True)

    st.divider()
    st.markdown("### 📋 Final Candidate Data Table")
    st.dataframe(
        master_df.style.map(
            lambda x: 'color: #d62728; font-weight: bold;' if x == 'Toxic' else 'color: #2ca02c;', subset=['Safety_Status']
        ).bar(subset=['Protegenicity', 'IFN_gamma_Score'], color='#5A9AF2'),
        use_container_width=True,
        hide_index=True
    )

# =============================================================================
# PUBLICATION FIGURES SECTION
# =============================================================================
def render_publication_figures():
    """Generate publication-quality figures for research papers."""
    st.title("📊 Publication Figures Dashboard")
    st.markdown("Customize and download high-resolution PNGs for your manuscript.")

    state = StateManager(DATA_DIR)
    counts = state.get_pipeline_counts()

    # --- Helper Functions ---
    def get_figure_data():
        """Load or generate data for publication figures."""
        data = {}
        
        # Helper to safely get counts
        input_count = counts.get("Input", "N/A")
        fragpipe_count = input_count if isinstance(input_count, int) else 450

        # Figure 1: Peptide Statistics
        # Get total peptides from fragpipe Excel file
        fragpipe_file = DATA_DIR / "combined_peptide_Lismo_Fragpipe.xlsx"
        total_uploaded = 0
        if fragpipe_file.exists():
            try:
                df = pd.read_excel(fragpipe_file)
                total_uploaded = len(df)
            except:
                total_uploaded = 0
        
        # Get validated/filtered peptides from peptide.fasta
        pep_file = DATA_DIR / "peptide.fasta"
        validated_peptides = 0
        if pep_file.exists():
            with open(pep_file, 'r') as f:
                validated_peptides = sum(1 for line in f if line.startswith(">"))
        
        # Use real data if available, otherwise mock
        if total_uploaded > 0 or validated_peptides > 0:
            data["fig1"] = pd.DataFrame({
                "Category": ["Total Uploaded Peptides", "Validated & Filtered Peptides"],
                "Count": [total_uploaded, validated_peptides]
            })
        else:
            data["fig1"] = pd.DataFrame({
                "Category": ["Total Uploaded Peptides", "Validated & Filtered Peptides"],
                "Count": [0, 0]
            })

        # Figure 2: Peptide Length Distribution
        # Try to get from peptide.fasta
        pep_file = DATA_DIR / "peptide.fasta"
        if pep_file.exists():
            lengths = []
            with open(pep_file, 'r') as f:
                for line in f:
                    if not line.startswith(">"):
                        lengths.append(len(line.strip()))
            if lengths:
                length_counts = pd.Series(lengths).value_counts().sort_index()
                data["fig2"] = pd.DataFrame({
                    "Category": length_counts.index,
                    "Count": length_counts.values
                })
            else:
                data["fig2"] = pd.DataFrame({"Category": ["No data"], "Count": [0]})
        else:
            data["fig2"] = pd.DataFrame({"Category": ["No data"], "Count": [0]})

        # Figure 3 & 4: MHC Binding Summary (Unique Peptides)
        mhc_strong_peps = set()
        mhc_weak_peps = set()
        all_peps = set()
        allele_binding = {}
        
        for mhc_dir in ["netmhcpan", "netmhciipan"]:
            mhc_file = DATA_DIR / mhc_dir / "netmhc_results.json"
            if mhc_file.exists():
                try:
                    with open(mhc_file, 'r') as f:
                        mhc_data = json.load(f)
                    for row in mhc_data:
                        pep = row.get('peptide', '')
                        allele = row.get('allele', 'Unknown')
                        if not pep: continue
                        all_peps.add(pep)
                        
                        rank = row.get('rank', 99.9)
                        try:
                            rank = float(rank)
                        except:
                            rank = 99.9
                            
                        # For Allele counts (for Venn diagram etc)
                        if allele not in allele_binding:
                            allele_binding[allele] = {"Strong": 0, "Weak": 0, "Non": 0}
                            
                        if rank <= 0.5:
                            mhc_strong_peps.add(pep)
                            allele_binding[allele]["Strong"] += 1
                        elif rank <= 2.0:
                            mhc_weak_peps.add(pep)
                            allele_binding[allele]["Weak"] += 1
                        else:
                            allele_binding[allele]["Non"] += 1
                except:
                    pass

        unique_strong = len(mhc_strong_peps)
        unique_weak = len(mhc_weak_peps - mhc_strong_peps)
        unique_non = len(all_peps - mhc_strong_peps - mhc_weak_peps)
        total_unique = len(all_peps)

        data["fig3"] = pd.DataFrame({
            "Category": ["Input Peptides", "Strong Binders", "Weak Binders", "Non Binders"],
            "Count": [total_unique, unique_strong, unique_weak, unique_non]
        })
        
        # Save top alleles for fig4
        allele_df = pd.DataFrame([
            {"Allele": a, "Strong Binders": c["Strong"]} 
            for a, c in allele_binding.items()
        ]).sort_values("Strong Binders", ascending=False)
        data["fig4_alleles"] = allele_df["Allele"].head(3).tolist() if not allele_df.empty else ["No data"]

        mhc_strong_count = unique_strong

        # Figure 5: Subcellular Localization (DeepLoc2)
        deeploc_file = DATA_DIR / "deeploc2" / "results.csv"
        all_locs = [
            'Cytoplasm', 'Nucleus', 'Extracellular', 'Cell membrane', 'Mitochondrion', 
            'Plastid', 'Endoplasmic reticulum', 'Lysosome/Vacuole', 'Golgi apparatus', 
            'Peroxisome', 'Peripheral', 'Transmembrane', 'Lipid anchor', 'Soluble', 'Signal peptide',
            'Nuclear localization signal'
        ]
        loc_counts = {k: 0 for k in all_locs}
        if deeploc_file.exists():
            try:
                df = pd.read_csv(deeploc_file)
                if 'Localizations' in df.columns:
                    for locs in df['Localizations'].dropna():
                        for k in ['Cytoplasm', 'Nucleus', 'Extracellular', 'Cell membrane', 'Mitochondrion', 'Plastid', 'Endoplasmic reticulum', 'Lysosome/Vacuole', 'Golgi apparatus', 'Peroxisome']:
                            if k.lower() in str(locs).lower(): loc_counts[k] += 1
                if 'Membrane types' in df.columns:
                    for mems in df['Membrane types'].dropna():
                        for k in ['Peripheral', 'Transmembrane', 'Lipid anchor', 'Soluble']:
                            if k.lower() in str(mems).lower(): loc_counts[k] += 1
                
                # Check for Signal Peptide and Nuclear Localization Signal in 'Signals' column
                if 'Signals' in df.columns:
                    for sigs in df['Signals'].dropna():
                        sigs_str = str(sigs).lower()
                        if 'signal peptide' in sigs_str or 'sp' in sigs_str:
                            loc_counts['Signal peptide'] += 1
                        if 'nuclear localization signal' in sigs_str or 'nls' in sigs_str:
                            loc_counts['Nuclear localization signal'] += 1
                elif 'Signal Peptide' in df.columns:
                    for sp in df['Signal Peptide'].dropna():
                        if str(sp).lower() in ['yes', 'true', '1']: loc_counts['Signal peptide'] += 1
            except: pass
            
        fig5_df = pd.DataFrame(list(loc_counts.items()), columns=["Category", "Count"])
        # Retain requested bars even if zero to fulfill specific visual requirements, and any >0
        requested_bars = ['Cell membrane', 'Mitochondrion', 'Plastid', 'Endoplasmic reticulum', 'Golgi apparatus', 'Peroxisome', 'Signal peptide', 'Nuclear localization signal']
        fig5_df = fig5_df[(fig5_df['Count'] > 0) | (fig5_df['Category'].isin(requested_bars))]
        data["fig5"] = fig5_df.sort_values("Count", ascending=False).reset_index(drop=True)
        if data["fig5"].empty:
            data["fig5"] = pd.DataFrame({"Category": ["No data"], "Count": [0]})

        # Figure 6: Antigenic Score of Peptides
        vax_file = DATA_DIR / "vaxign" / "vaxign.result.tsv"
        if vax_file.exists():
            try:
                vax_df = pd.read_csv(vax_file, sep='\t')
                if 'sample' in vax_df.columns and 'prediction' in vax_df.columns:
                    data["fig6"] = vax_df[['sample', 'prediction']].head(20).copy()
                    data["fig6"]["Antigenic_Score"] = data["fig6"]["prediction"]
                    data["fig6"] = data["fig6"].rename(columns={'sample': 'Peptide'})
            except: pass
            
        if "fig6" not in data or data["fig6"].empty:
            data["fig6"] = pd.DataFrame({"Peptide": ["No data"], "Antigenic_Score": [0.0]})
            
        vax_count = counts.get("Vaxign", "N/A")
        antigenic_count = vax_count if isinstance(vax_count, int) else 0

        # Figure 7: Allergenicity
        alg_file = DATA_DIR / "algpred" / "algpred" / "algpred_results.csv"
        total_alg = 0; non_alg = 0; alg = 0
        if alg_file.exists():
            try:
                with open(alg_file, 'r') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        total_alg += 1
                        if row.get('Prediction') == 'Non-Allergen': non_alg += 1
                        else: alg += 1
            except: pass
        data["fig7"] = pd.DataFrame({
            "Category": ["Input (Antigenic Peptides)", "Non-Allergen", "Allergen"], 
            "Count": [total_alg if alg_file.exists() else antigenic_count, non_alg, alg]
        })

        # Figure 8: Toxicity
        tox_file = DATA_DIR / "toxinpred" / "toxinpred" / "toxinpred_results.csv"
        total_tox = 0; non_tox = 0; tox = 0
        if tox_file.exists():
            try:
                with open(tox_file, 'r') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        total_tox += 1
                        if row.get('Prediction') == 'Non-Toxin': non_tox += 1
                        else: tox += 1
            except: pass
        data["fig8"] = pd.DataFrame({
            "Category": ["Input (Non-Allergens)", "Non-Toxic", "Toxic"], 
            "Count": [total_tox if tox_file.exists() else non_alg, non_tox, tox]
        })

        # Figure 9: Cytokines (IL2 / IFNEpitope)
        total_safe = 0
        safe_file = DATA_DIR / "inputs" / "safe_peptides.fasta"
        if safe_file.exists():
            try:
                with open(safe_file, 'r') as f:
                    total_safe = sum(1 for line in f if line.startswith(">"))
            except: pass
        
        il2_count = 0
        il2_file = DATA_DIR / "il2pred" / "il2pred" / "il2pred_results.csv"
        if il2_file.exists():
            try:
                with open(il2_file, 'r') as f:
                    reader = csv.DictReader(f)
                    il2_count = sum(1 for row in reader if row.get('Prediction', '').strip().lower() == 'il2 inducing')
            except: pass
        
        ifn_count = 0
        ifn_file = DATA_DIR / "ifnepitope" / "ifnepitope" / "ifnepitope_results.csv"
        if ifn_file.exists():
            try:
                with open(ifn_file, 'r') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        pred = row.get('Prediction', '').strip().lower()
                        if pred == 'ifn-γ inducer' or pred == 'inducer':
                            ifn_count += 1
            except: pass
        
        data["fig9"] = pd.DataFrame({
            "Category": ["Input (Non-Toxic Peptides)", "IL-2 Inducers", "IFN-γ Inducers"],
            "Count": [total_safe if safe_file.exists() else non_tox, il2_count, ifn_count]
        })

        # Figure 10: Pipeline Summary
        data["fig10"] = pd.DataFrame({
            "Category": [
                "Filtered Input Peptides",
                "MHC Strong Binders",
                "Antigenic Peptides",
                "Non-Allergenic Peptides",
                "Non-Toxic Peptides",
                "IL-2 Inducers",
                "IFN-γ Inducers"
            ],
            "Count": [
                fragpipe_count,
                mhc_strong_count,
                antigenic_count,
                non_alg,
                non_tox,
                il2_count,
                ifn_count
            ]
        })

        return data

    data = get_figure_data()

    # ---------------------------------------------------------
    # Figure 1: Peptide Statistics (2 bars, different colors)
    # ---------------------------------------------------------
    st.markdown("### Figure 1: Peptide Statistics")
    with st.expander("Customize Figure 1"):
        c1, c2 = st.columns(2)
        f1_title = c1.text_input("Title", "Peptide Statistics: Uploaded vs Validated", key="pub_f1_t")
        f1_xtitle = c2.text_input("X-axis Title", "Category", key="pub_f1_xt")
        c3, c4 = st.columns(2)
        f1_ytitle = c3.text_input("Y-axis Title", "Peptide Count", key="pub_f1_yt")
        f1_fs = c4.slider("Font Size", 8, 24, 14, key="pub_f1_fs")
        f1_bw = st.slider("Bar Width", 0.1, 1.0, 0.6, key="pub_f1_bw")
        c5, c6 = st.columns(2)
        f1_w = c5.number_input("Width (px)", 200, 1600, 800, key="pub_f1_w")
        f1_h = c6.number_input("Height (px)", 200, 1600, 600, key="pub_f1_h")
        c7, c8, c_lbl = st.columns([1, 1, 1])
        f1_xgrid = c7.checkbox("X-axis inner grey lines", True, key="pub_f1_xg")
        f1_ygrid = c8.checkbox("Y-axis inner grey lines", True, key="pub_f1_yg")
        f1_lbl_pos = c_lbl.selectbox("Label Position", ["inside", "outside", "none"], index=0, key="pub_f1_lbl")
        f1_broken = st.checkbox("Broken Y-Axis (if diff > 500)", False, key="pub_f1_broken")
        
        st.markdown("**Bar Colors**")
        color_cols = st.columns(4)
        f1_colors = []
        default_colors = [color_to_hex(c) for c in px.colors.qualitative.Pastel1]
        for i, cat in enumerate(data["fig1"]["Category"]):
            f1_colors.append(color_cols[i % 4].color_picker(str(cat), default_colors[i % len(default_colors)], key=f"pub_f1_c_{i}"))

    # Check if broken axis should be applied
    y_values = data["fig1"]["Count"].values
    y_max = max(y_values)
    y_min = min(y_values)
    y_diff = y_max - y_min
    
    apply_broken = f1_broken and y_diff > 500
    
    fig1 = go.Figure()
    fig1.add_trace(go.Bar(
        x=data["fig1"]["Category"],
        y=data["fig1"]["Count"],
        marker_color=f1_colors,
        text=[str(int(x)) for x in data["fig1"]["Count"]] if f1_lbl_pos != "none" else None,
        textposition=f1_lbl_pos if f1_lbl_pos != "none" else None
    ))
    
    if apply_broken:
        # Use dual y-axis subplot approach for proper broken axis
        fig1 = make_subplots(
            rows=2, cols=1, 
            shared_xaxes=True, 
            vertical_spacing=0.08,
            row_heights=[0.7, 0.3]
        )
        
        # Add the same bar data to BOTH subplots
        for i in [1, 2]:
            fig1.add_trace(go.Bar(
                x=data["fig1"]["Category"],
                y=data["fig1"]["Count"],
                marker_color=f1_colors,
                text=[str(int(x)) for x in data["fig1"]["Count"]] if f1_lbl_pos != "none" else None,
                textposition=f1_lbl_pos if f1_lbl_pos != "none" else None,
                showlegend=False
            ), row=i, col=1)
        
        # Top Plot: Focus on the large values (with some padding)
        fig1.update_yaxes(range=[y_max * 0.95, y_max * 1.05], row=1, col=1, 
                         title=f1_ytitle, showgrid=f1_ygrid, gridcolor='lightgray')
        
        # Bottom Plot: Focus on the small values (with some padding)
        fig1.update_yaxes(range=[0, y_min * 2.5] if y_min > 0 else [0, 50], row=2, col=1,
                         title=f1_ytitle, showgrid=f1_ygrid, gridcolor='lightgray')
        
        # Update x-axis on bottom plot only
        fig1.update_xaxes(title=f1_xtitle, showgrid=f1_xgrid, gridcolor='lightgray', row=2, col=1)
        
        # Add break indicator annotation
        fig1.add_annotation(
            x=0, y=1.15, xref="paper", yref="paper",
            text="///", showarrow=False, 
            font=dict(size=24, color="black"),
            ax=0, ay=0
        )
        
        fig1.update_layout(
            title=f1_title, template=PUB_THEME,
            font=dict(family="Arial", size=f1_fs, color="black"),
            width=f1_w, height=f1_h, bargap=0.4
        )
    else:
        fig1.update_layout(
            title=f1_title, template=PUB_THEME, 
            font=dict(family="Arial", size=f1_fs, color="black"),
            xaxis=dict(title=f1_xtitle, showgrid=f1_xgrid, gridcolor='lightgray'),
            yaxis=dict(title=f1_ytitle, showgrid=f1_ygrid, gridcolor='lightgray'),
            width=f1_w, height=f1_h, bargroupgap=max(0, f1_bw-0.5)
        )
    st.plotly_chart(fig1, use_container_width=False, config=PUB_CONFIG)

    # ---------------------------------------------------------
    # Figure 2: Peptide Length Distribution (different colors, labels, y-axis gray lines)
    # ---------------------------------------------------------
    st.markdown("### Figure 2: Pathogen Peptide Lengths")
    with st.expander("Customize Figure 2"):
        c1, c2 = st.columns(2)
        f2_title = c1.text_input("Title", "Peptide Length Distribution", key="pub_f2_t")
        f2_xtitle = c2.text_input("X-axis Title", "Peptide Length", key="pub_f2_xt")
        c3, c4 = st.columns(2)
        f2_ytitle = c3.text_input("Y-axis Title", "Count", key="pub_f2_yt")
        f2_fs = c4.slider("Font Size", 8, 24, 14, key="pub_f2_fs")
        f2_bw = st.slider("Bar Width", 0.1, 1.0, 0.6, key="pub_f2_bw")
        c5, c6 = st.columns(2)
        f2_w = c5.number_input("Width (px)", 200, 1600, 800, key="pub_f2_w")
        f2_h = c6.number_input("Height (px)", 200, 1600, 600, key="pub_f2_h")
        c7, c8, c_lbl = st.columns([1, 1, 1])
        f2_xgrid = c7.checkbox("X-axis inner grey lines", True, key="pub_f2_xg")
        f2_ygrid = c8.checkbox("Y-axis inner grey lines", True, key="pub_f2_yg")
        f2_lbl_pos = c_lbl.selectbox("Label Position", ["inside", "outside", "none"], index=1, key="pub_f2_lbl")
        f2_legend = st.checkbox("Show legend", False, key="pub_f2_leg")
        f2_broken = st.checkbox("Broken Y-Axis (if diff > 500)", False, key="pub_f2_broken")
        
        st.markdown("**Bar Colors**")
        color_cols = st.columns(7)
        f2_colors = []
        default_colors = [color_to_hex(c) for c in px.colors.qualitative.Pastel1 + px.colors.qualitative.Set3]
        for i, cat in enumerate(data["fig2"]["Category"]):
            f2_colors.append(color_cols[i % 7].color_picker(str(cat), default_colors[i % len(default_colors)], key=f"pub_f2_c_{i}"))

    fig2 = go.Figure()
    for i, row in data["fig2"].iterrows():
        fig2.add_trace(go.Bar(
            x=[row["Category"]], y=[row["Count"]], 
            name=str(row["Category"]),
            marker_color=f2_colors[i],
            text=[str(int(row["Count"]))] if f2_lbl_pos != "none" else None,
            textposition=f2_lbl_pos if f2_lbl_pos != "none" else None
        ))
    
    # Check for broken axis
    y_values = data["fig2"]["Count"].values
    y_max = max(y_values)
    y_min = min(y_values)
    apply_broken = f2_broken and (y_max - y_min) > 500
    
    if apply_broken:
        # Use dual y-axis subplot approach for proper broken axis
        fig2 = make_subplots(
            rows=2, cols=1, 
            shared_xaxes=True, 
            vertical_spacing=0.08,
            row_heights=[0.7, 0.3]
        )
        
        # Add the same bar data to BOTH subplots
        for i in [1, 2]:
            for idx, row in data["fig2"].iterrows():
                fig2.add_trace(go.Bar(
                    x=[row["Category"]],
                    y=[row["Count"]],
                    name=str(row["Category"]),
                    marker_color=f2_colors[idx],
                    text=[str(int(row["Count"]))] if f2_lbl_pos != "none" else None,
                    textposition=f2_lbl_pos if f2_lbl_pos != "none" else None,
                    showlegend=False
                ), row=i, col=1)
        
        # Top Plot: Focus on the large values (with some padding)
        fig2.update_yaxes(range=[y_max * 0.95, y_max * 1.05], row=1, col=1, 
                         title=f2_ytitle, showgrid=f2_ygrid, gridcolor='lightgray')
        
        # Bottom Plot: Focus on the small values (with some padding)
        fig2.update_yaxes(range=[0, y_min * 2.5] if y_min > 0 else [0, 50], row=2, col=1,
                         title=f2_ytitle, showgrid=f2_ygrid, gridcolor='lightgray')
        
        # Update x-axis on bottom plot only
        fig2.update_xaxes(title=f2_xtitle, showgrid=f2_xgrid, gridcolor='lightgray', dtick=1, row=2, col=1)
        
        # Add break indicator annotation
        fig2.add_annotation(
            x=0, y=1.15, xref="paper", yref="paper",
            text="///", showarrow=False, 
            font=dict(size=24, color="black"),
            ax=0, ay=0
        )
        
        fig2.update_layout(
            title=f2_title, template=PUB_THEME,
            font=dict(family="Arial", size=f2_fs, color="black"),
            width=f2_w, height=f2_h, bargap=0.4,
            showlegend=f2_legend
        )
    else:
        fig2.update_layout(
            title=f2_title, template=PUB_THEME,
            font=dict(family="Arial", size=f2_fs, color="black"),
            xaxis=dict(title=f2_xtitle, showgrid=f2_xgrid, gridcolor='lightgray', dtick=1),
            yaxis=dict(title=f2_ytitle, showgrid=f2_ygrid, gridcolor='lightgray'),
            showlegend=f2_legend, width=f2_w, height=f2_h, bargroupgap=max(0, f2_bw-0.5)
        )
    st.plotly_chart(fig2, use_container_width=False, config=PUB_CONFIG)

    # ---------------------------------------------------------
    # Figure 3: MHC Binding - 4 bars (Total, Strong, Weak, Non-binders)
    # ---------------------------------------------------------
    st.markdown("### Figure 3: MHC Binding Summary")

    fig3_data = data["fig3"].copy()
    y_values3 = fig3_data["Count"].values
    y_max3 = max(y_values3) if len(y_values3) > 0 else 100
    y_min3 = min(y_values3) if len(y_values3) > 0 else 0

    with st.expander("Customize Figure 3"):
        c1, c2 = st.columns(2)
        f3_title = c1.text_input("Title", "MHC Binding Summary", key="pub_f3_t")
        f3_xtitle = c2.text_input("X-axis Title", "Binding Category", key="pub_f3_xt")
        c3, c4 = st.columns(2)
        f3_ytitle = c3.text_input("Y-axis Title", "Peptide Count", key="pub_f3_yt")
        f3_fs = c4.slider("Font Size", 8, 24, 14, key="pub_f3_fs")
        f3_bw = st.slider("Bar Width", 0.1, 1.0, 0.6, key="pub_f3_bw")
        c5, c6 = st.columns(2)
        f3_w = c5.number_input("Width (px)", 200, 1600, 800, key="pub_f3_w")
        f3_h = c6.number_input("Height (px)", 200, 1600, 600, key="pub_f3_h")
        c7, c8, c_lbl = st.columns([1, 1, 1])
        f3_xgrid = c7.checkbox("X-axis inner grey lines", True, key="pub_f3_xg")
        f3_ygrid = c8.checkbox("Y-axis inner grey lines", True, key="pub_f3_yg")
        f3_lbl_pos = c_lbl.selectbox("Label Position", ["inside", "outside", "none"], index=0, key="pub_f3_lbl")
        f3_legend = st.checkbox("Show legend", True, key="pub_f3_leg")
        
        st.markdown("**Bar Colors**")
        color_cols = st.columns(4)
        f3_colors = []
        default_colors = ["#AED6F1", "#F5B7B1", "#F9E79F", "#D7DBDD"]
        for i, cat in enumerate(fig3_data["Category"]):
            f3_colors.append(color_cols[i % 4].color_picker(str(cat), default_colors[i % len(default_colors)], key=f"pub_f3_c_{i}"))
        f3_broken = st.checkbox("Enable Broken Y-Axis", False, key="pub_f3_broken")
        if f3_broken:
            c9, c10, c11 = st.columns(3)
            f3_bot_max = c9.number_input("Bottom Axis Max", value=float(y_min3 * 2.5 if y_min3 > 0 else 50), key="pub_f3_bmax")
            f3_top_min = c10.number_input("Top Axis Min", value=float(y_max3 * 0.8), key="pub_f3_tmin")
            f3_top_max = c11.number_input("Top Axis Max", value=float(y_max3 * 1.1), key="pub_f3_tmax")

    fig3 = go.Figure()
    fig3.add_trace(go.Bar(
        x=fig3_data["Category"], y=fig3_data["Count"],
        marker_color=f3_colors,
        text=[str(int(x)) for x in fig3_data["Count"]] if f3_lbl_pos != "none" else None,
        textposition=f3_lbl_pos if f3_lbl_pos != "none" else None
    ))
    
    if f3_broken:
        # Use dual y-axis subplot approach for proper broken axis
        fig3 = make_subplots(
            rows=2, cols=1, 
            shared_xaxes=True, 
            vertical_spacing=0.08,
            row_heights=[0.7, 0.3]
        )
        
        # Add the same bar data to BOTH subplots
        for i in [1, 2]:
            fig3.add_trace(go.Bar(
                x=fig3_data["Category"], y=fig3_data["Count"],
                marker_color=f3_colors,
                text=[str(int(x)) for x in fig3_data["Count"]] if f3_lbl_pos != "none" else None,
                textposition=f3_lbl_pos if f3_lbl_pos != "none" else None,
                showlegend=False
            ), row=i, col=1)
        
        # Top Plot: Focus on the large values (with some padding)
        fig3.update_yaxes(range=[f3_top_min, f3_top_max], row=1, col=1, 
                         title=f3_ytitle, showgrid=f3_ygrid, gridcolor='lightgray')
        
        # Bottom Plot: Focus on the small values (with some padding)
        fig3.update_yaxes(range=[0, f3_bot_max], row=2, col=1,
                         title=f3_ytitle, showgrid=f3_ygrid, gridcolor='lightgray')
        
        # Update x-axis on bottom plot only
        fig3.update_xaxes(title=f3_xtitle, showgrid=f3_xgrid, gridcolor='lightgray', row=2, col=1)
        
        # Add break indicator annotation
        fig3.add_annotation(
            x=0, y=1.15, xref="paper", yref="paper",
            text="///", showarrow=False, 
            font=dict(size=24, color="black"),
            ax=0, ay=0
        )
        
        fig3.update_layout(
            title=f3_title, template=PUB_THEME,
            font=dict(family="Arial", size=f3_fs, color="black"),
            width=f3_w, height=f3_h, bargap=0.4,
            showlegend=f3_legend
        )
    else:
        fig3.update_layout(
            title=f3_title, template=PUB_THEME,
            font=dict(family="Arial", size=f3_fs, color="black"),
            xaxis=dict(title=f3_xtitle, showgrid=f3_xgrid, gridcolor='lightgray'),
            yaxis=dict(title=f3_ytitle, showgrid=f3_ygrid, gridcolor='lightgray'),
            showlegend=f3_legend, width=f3_w, height=f3_h, bargroupgap=max(0, f3_bw-0.5)
        )
    st.plotly_chart(fig3, use_container_width=False, config=PUB_CONFIG)

    # ---------------------------------------------------------
    # Figure 4: Strong Binders by Allele Count (Horizontal Bar)
    # ---------------------------------------------------------
    st.markdown("### Figure 4: Strong Binders by Allele Count")
    
    with st.expander("Customize Figure 4"):
        c1, c2 = st.columns(2)
        f4_title = c1.text_input("Title", "Peptides: Strong Binding Across Alleles", key="pub_f4_t")
        f4_xtitle = c2.text_input("X-axis Title", "Number of Alleles (Strong Binders)", key="pub_f4_xt")
        c3, c4 = st.columns(2)
        f4_ytitle = c3.text_input("Y-axis Title", "Peptide", key="pub_f4_yt")
        f4_fs = c4.slider("Font Size", 8, 24, 14, key="pub_f4_fs")
        c5, c6 = st.columns(2)
        f4_w = c5.number_input("Width (px)", 200, 1600, 1000, key="pub_f4_w")
        f4_h = c6.number_input("Height (px)", 200, 1600, 600, key="pub_f4_h")
        c7, c8 = st.columns(2)
        f4_xgrid = c7.checkbox("X-axis inner grey lines", True, key="pub_f4_xg")
        f4_ygrid = c8.checkbox("Y-axis inner grey lines", True, key="pub_f4_yg")
        f4_color = c8.color_picker("Bar Color", "#AED6F1", key="pub_f4_c")
        f4_max_bars = c7.number_input("Max Bars to Display", 10, 500, 50, key="pub_f4_max")

    # Get strong binders and count alleles per peptide
    peptide_alleles = {}
    for mhc_dir in ["netmhcpan", "netmhciipan"]:
        mhc_file = DATA_DIR / mhc_dir / "netmhc_results.json"
        if mhc_file.exists():
            try:
                with open(mhc_file, 'r') as f:
                    mhc_data = json.load(f)
                for row in mhc_data:
                    peptide = row.get('peptide', '')
                    allele = row.get('allele', '')
                    rank = row.get('rank', 99.9)
                    try:
                        rank = float(rank)
                    except:
                        rank = 99.9
                    if rank <= 0.5 and peptide:  # Strong binder
                        if peptide not in peptide_alleles:
                            peptide_alleles[peptide] = set()
                        peptide_alleles[peptide].add(allele)
            except:
                pass
    
    # Create dataframe from peptide-allele counts
    if len(peptide_alleles) > 0:
        fig4_data = pd.DataFrame([
            {"Peptide": pep, "Allele_Count": len(alleles)}
            for pep, alleles in peptide_alleles.items()
        ])
        # Sort by allele count (descending) and limit to max bars
        fig4_data = fig4_data.sort_values("Allele_Count", ascending=True).tail(f4_max_bars)
    else:
        fig4_data = pd.DataFrame({
            "Peptide": ["No data"],
            "Allele_Count": [0]
        })
    
    # Create horizontal bar chart
    fig4 = go.Figure()
    fig4.add_trace(go.Bar(
        y=fig4_data["Peptide"],
        x=fig4_data["Allele_Count"],
        orientation='h',
        marker_color=f4_color,
        text=[str(int(x)) for x in fig4_data["Allele_Count"]],
        textposition='auto'
    ))
    
    fig4.update_layout( 
        title=f4_title, template=PUB_THEME,
        font=dict(family="Arial", size=f4_fs, color="black"),
        xaxis=dict(title=f4_xtitle, showgrid=f4_xgrid, gridcolor='lightgray'),
        yaxis=dict(title=f4_ytitle, showgrid=f4_ygrid, gridcolor='lightgray'),
        width=f4_w, height=f4_h,
        hovermode='closest'
    )
    st.plotly_chart(fig4, use_container_width=False, config=PUB_CONFIG)

    # ---------------------------------------------------------
    # Figure 5: Subcellular Localization (DeepLoc2)
    # ---------------------------------------------------------
    st.markdown("### Figure 5: Subcellular Localization")
    fig5_data = data["fig5"].copy()
    y_values5 = fig5_data["Count"].values
    y_max5 = max(y_values5) if len(y_values5) > 0 else 100
    y_min5 = min(y_values5) if len(y_values5) > 0 else 0

    with st.expander("Customize Figure 5"):
        c1, c2 = st.columns(2)
        f5_title = c1.text_input("Title", "Localization Domains", key="pub_f5_t")
        f5_xtitle = c2.text_input("X-axis Title", "Domain", key="pub_f5_xt")
        c3, c4 = st.columns(2)
        f5_ytitle = c3.text_input("Y-axis Title", "Protein Count", key="pub_f5_yt")
        f5_fs = c4.slider("Font Size", 8, 24, 14, key="pub_f5_fs")
        f5_bw = st.slider("Bar Width", 0.1, 1.0, 0.6, key="pub_f5_bw")
        c5, c6 = st.columns(2)
        f5_w = c5.number_input("Width (px)", 200, 1600, 800, key="pub_f5_w")
        f5_h = c6.number_input("Height (px)", 200, 1600, 600, key="pub_f5_h")
        c7, c8, c_lbl = st.columns([1, 1, 1])
        f5_xgrid = c7.checkbox("X-axis inner grey lines", True, key="pub_f5_xg")
        f5_ygrid = c8.checkbox("Y-axis inner grey lines", True, key="pub_f5_yg")
        f5_lbl_pos = c_lbl.selectbox("Label Position", ["inside", "outside", "none"], index=1, key="pub_f5_lbl")
        f5_legend = st.checkbox("Show legend", False, key="pub_f5_leg")
        f5_broken = st.checkbox("Enable Broken Y-Axis", False, key="pub_f5_broken")
        if f5_broken:
            c9, c10, c11 = st.columns(3)
            f5_bot_max = c9.number_input("Bottom Axis Max", value=float(y_min5 * 2.5 if y_min5 > 0 else 50), key="pub_f5_bmax")
            f5_top_min = c10.number_input("Top Axis Min", value=float(y_max5 * 0.8), key="pub_f5_tmin")
            f5_top_max = c11.number_input("Top Axis Max", value=float(y_max5 * 1.1), key="pub_f5_tmax")

        st.markdown("**Bar Colors**")
        color_cols = st.columns(5)
        f5_colors = []
        default_colors = [color_to_hex(c) for c in px.colors.qualitative.Pastel1 + px.colors.qualitative.Set3 + px.colors.qualitative.Set2]
        for i, cat in enumerate(fig5_data["Category"]):
            f5_colors.append(color_cols[i % 5].color_picker(str(cat)[:15], default_colors[i % len(default_colors)], key=f"pub_f5_c_{i}"))

    fig5 = go.Figure()
    fig5.add_trace(go.Bar(
        x=fig5_data["Category"], y=fig5_data["Count"],
        marker_color=f5_colors,
        text=[str(int(x)) for x in fig5_data["Count"]] if f5_lbl_pos != "none" else None,
        textposition=f5_lbl_pos if f5_lbl_pos != "none" else None
    ))
    
    if f5_broken:
        fig5 = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08, row_heights=[0.7, 0.3])
        for i in [1, 2]:
            fig5.add_trace(go.Bar(
                x=fig5_data["Category"], y=fig5_data["Count"],
                marker_color=f5_colors,
                text=[str(int(x)) for x in fig5_data["Count"]] if f5_lbl_pos != "none" else None,
                textposition=f5_lbl_pos if f5_lbl_pos != "none" else None,
                showlegend=False
            ), row=i, col=1)
        fig5.update_yaxes(range=[f5_top_min, f5_top_max], row=1, col=1, title=f5_ytitle, showgrid=f5_ygrid, gridcolor='lightgray')
        fig5.update_yaxes(range=[0, f5_bot_max], row=2, col=1, title=f5_ytitle, showgrid=f5_ygrid, gridcolor='lightgray')
        fig5.update_xaxes(title=f5_xtitle, showgrid=f5_xgrid, gridcolor='lightgray', row=2, col=1)
        fig5.add_annotation(x=0, y=1.15, xref="paper", yref="paper", text="///", showarrow=False, font=dict(size=24, color="black"), ax=0, ay=0)
        fig5.update_layout(title=f5_title, template=PUB_THEME, font=dict(family="Arial", size=f5_fs, color="black"), width=f5_w, height=f5_h, bargap=0.4, showlegend=f5_legend)
    else:
        fig5.update_layout(title=f5_title, template=PUB_THEME, font=dict(family="Arial", size=f5_fs, color="black"),
            xaxis=dict(title=f5_xtitle, showgrid=f5_xgrid, gridcolor='lightgray'),
            yaxis=dict(title=f5_ytitle, showgrid=f5_ygrid, gridcolor='lightgray'),
            showlegend=f5_legend, width=f5_w, height=f5_h, bargroupgap=max(0, f5_bw-0.5))
    st.plotly_chart(fig5, use_container_width=False, config=PUB_CONFIG)

    # ---------------------------------------------------------
    # Figure 6: Antigenic Score of Peptides
    # ---------------------------------------------------------
    st.markdown("### Figure 6: Antigenic Score of Peptides")
    with st.expander("Customize Figure 6"):
        c1, c2 = st.columns(2)
        f6_title = c1.text_input("Title", "Antigenic Score by Peptide", key="pub_f6_t")
        f6_xtitle = c2.text_input("X-axis Title", "Antigenic Score", key="pub_f6_xt")
        c3, c4 = st.columns(2)
        f6_ytitle = c3.text_input("Y-axis Title", "Peptide Sequence", key="pub_f6_yt")
        f6_fs = c4.slider("Font Size", 8, 24, 12, key="pub_f6_fs")
        f6_bw = st.slider("Bar Width", 0.1, 1.0, 0.4, key="pub_f6_bw")
        c5, c6 = st.columns(2)
        f6_w = c5.number_input("Width (px)", 200, 1600, 800, key="pub_f6_w")
        f6_h = c6.number_input("Height (px)", 200, 1600, 600, key="pub_f6_h")
        c7, c8, c_lbl = st.columns([1, 1, 1])
        f6_xgrid = c7.checkbox("X-axis inner grey lines", True, key="pub_f6_xg")
        f6_ygrid = c8.checkbox("Y-axis inner grey lines", True, key="pub_f6_yg")
        f6_lbl_pos = c_lbl.selectbox("Label Position", ["inside", "outside", "none"], index=1, key="pub_f6_lbl")

        st.markdown("**Bar Colors**")
        color_cols = st.columns(5)
        f6_colors = []
        default_colors = [color_to_hex(c) for c in px.colors.qualitative.Pastel1 + px.colors.qualitative.Set3 + px.colors.qualitative.Set2]
        for i, cat in enumerate(data["fig6"]["Peptide"]):
            f6_colors.append(color_cols[i % 5].color_picker(str(cat)[:10], default_colors[i % len(default_colors)], key=f"pub_f6_c_{i}"))

    fig6_df = data["fig6"].sort_values("Antigenic_Score", ascending=True) # Lowest at top

    fig6 = go.Figure()
    fig6.add_trace(go.Bar(
        y=fig6_df["Peptide"], x=fig6_df["Antigenic_Score"],
        orientation='h',
        marker_color=f6_colors,
        width=f6_bw,
        text=[str(round(x, 2)) for x in fig6_df["Antigenic_Score"]] if f6_lbl_pos != "none" else None,
        textposition=f6_lbl_pos if f6_lbl_pos != "none" else None
    ))
    fig6.update_layout(title=f6_title, template=PUB_THEME, font=dict(family="Arial", size=f6_fs, color="black"),
        xaxis=dict(title=f6_xtitle, showgrid=f6_xgrid, gridcolor='lightgray'),
        yaxis=dict(title=f6_ytitle, showgrid=f6_ygrid, gridcolor='lightgray'),
        width=f6_w, height=f6_h)
    st.plotly_chart(fig6, use_container_width=False, config=PUB_CONFIG)

    # ---------------------------------------------------------
    # Figure 7: Allergenicity Profile
    # ---------------------------------------------------------
    st.markdown("### Figure 7: Allergenicity Profile")
    fig7_data = data["fig7"].copy()
    y_values7 = fig7_data["Count"].values
    y_max7 = max(y_values7) if len(y_values7) > 0 else 100
    y_min7 = min(y_values7) if len(y_values7) > 0 else 0

    with st.expander("Customize Figure 7"):
        c1, c2 = st.columns(2)
        f7_title = c1.text_input("Title", "Allergenicity Profile (AlgPred)", key="pub_f7_t")
        f7_xtitle = c2.text_input("X-axis Title", "Category", key="pub_f7_xt")
        c3, c4 = st.columns(2)
        f7_ytitle = c3.text_input("Y-axis Title", "Peptide Count", key="pub_f7_yt")
        f7_fs = c4.slider("Font Size", 8, 24, 14, key="pub_f7_fs")
        f7_bw = st.slider("Bar Width", 0.1, 1.0, 0.6, key="pub_f7_bw")
        c5, c6 = st.columns(2)
        f7_w = c5.number_input("Width (px)", 200, 1600, 800, key="pub_f7_w")
        f7_h = c6.number_input("Height (px)", 200, 1600, 600, key="pub_f7_h")
        c7, c8, c_lbl = st.columns([1, 1, 1])
        f7_xgrid = c7.checkbox("X-axis inner grey lines", True, key="pub_f7_xg")
        f7_ygrid = c8.checkbox("Y-axis inner grey lines", True, key="pub_f7_yg")
        f7_lbl_pos = c_lbl.selectbox("Label Position", ["inside", "outside", "none"], index=1, key="pub_f7_lbl")
        f7_legend = st.checkbox("Show legend", True, key="pub_f7_leg")
        f7_broken = st.checkbox("Enable Broken Y-Axis", False, key="pub_f7_broken")
        if f7_broken:
            c9, c10, c11 = st.columns(3)
            f7_bot_max = c9.number_input("Bottom Axis Max", value=float(y_min7 * 2.5 if y_min7 > 0 else 50), key="pub_f7_bmax")
            f7_top_min = c10.number_input("Top Axis Min", value=float(y_max7 * 0.8), key="pub_f7_tmin")
            f7_top_max = c11.number_input("Top Axis Max", value=float(y_max7 * 1.1), key="pub_f7_tmax")

        st.markdown("**Bar Colors**")
        color_cols = st.columns(3)
        f7_colors = []
        default_colors = ["#AED6F1", "#ABEBC6", "#F5B7B1"] # Blue, Green, Red
        for i, cat in enumerate(fig7_data["Category"]):
            f7_colors.append(color_cols[i % 3].color_picker(str(cat)[:15], default_colors[i % len(default_colors)], key=f"pub_f7_c_{i}"))

    fig7 = go.Figure()
    fig7.add_trace(go.Bar(
        x=fig7_data["Category"], y=fig7_data["Count"],
        marker_color=f7_colors,
        text=[str(int(x)) for x in fig7_data["Count"]] if f7_lbl_pos != "none" else None,
        textposition=f7_lbl_pos if f7_lbl_pos != "none" else None
    ))
    
    if f7_broken:
        fig7 = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08, row_heights=[0.7, 0.3])
        for i in [1, 2]:
            fig7.add_trace(go.Bar(
                x=fig7_data["Category"], y=fig7_data["Count"],
                marker_color=f7_colors,
                text=[str(int(x)) for x in fig7_data["Count"]] if f7_lbl_pos != "none" else None,
                textposition=f7_lbl_pos if f7_lbl_pos != "none" else None,
                showlegend=False
            ), row=i, col=1)
        fig7.update_yaxes(range=[f7_top_min, f7_top_max], row=1, col=1, title=f7_ytitle, showgrid=f7_ygrid, gridcolor='lightgray')
        fig7.update_yaxes(range=[0, f7_bot_max], row=2, col=1, title=f7_ytitle, showgrid=f7_ygrid, gridcolor='lightgray')
        fig7.update_xaxes(title=f7_xtitle, showgrid=f7_xgrid, gridcolor='lightgray', row=2, col=1)
        fig7.add_annotation(x=0, y=1.15, xref="paper", yref="paper", text="///", showarrow=False, font=dict(size=24, color="black"), ax=0, ay=0)
        fig7.update_layout(title=f7_title, template=PUB_THEME, font=dict(family="Arial", size=f7_fs, color="black"), width=f7_w, height=f7_h, bargap=0.4, showlegend=f7_legend)
    else:
        fig7.update_layout(title=f7_title, template=PUB_THEME, font=dict(family="Arial", size=f7_fs, color="black"),
            xaxis=dict(title=f7_xtitle, showgrid=f7_xgrid, gridcolor='lightgray'),
            yaxis=dict(title=f7_ytitle, showgrid=f7_ygrid, gridcolor='lightgray'),
            showlegend=f7_legend, width=f7_w, height=f7_h, bargroupgap=max(0, f7_bw-0.5))
    st.plotly_chart(fig7, use_container_width=False, config=PUB_CONFIG)

    # ---------------------------------------------------------
    # Figure 8: Toxicity Profile
    # ---------------------------------------------------------
    st.markdown("### Figure 8: Toxicity Profile")
    fig8_data = data["fig8"].copy()
    y_values8 = fig8_data["Count"].values
    y_max8 = max(y_values8) if len(y_values8) > 0 else 100
    y_min8 = min(y_values8) if len(y_values8) > 0 else 0

    with st.expander("Customize Figure 8"):
        c1, c2 = st.columns(2)
        f8_title = c1.text_input("Title", "Toxicity Profile (ToxinPred)", key="pub_f8_t")
        f8_xtitle = c2.text_input("X-axis Title", "Category", key="pub_f8_xt")
        c3, c4 = st.columns(2)
        f8_ytitle = c3.text_input("Y-axis Title", "Peptide Count", key="pub_f8_yt")
        f8_fs = c4.slider("Font Size", 8, 24, 14, key="pub_f8_fs")
        f8_bw = st.slider("Bar Width", 0.1, 1.0, 0.6, key="pub_f8_bw")
        c5, c6 = st.columns(2)
        f8_w = c5.number_input("Width (px)", 200, 1600, 800, key="pub_f8_w")
        f8_h = c6.number_input("Height (px)", 200, 1600, 600, key="pub_f8_h")
        c7, c8, c_lbl = st.columns([1, 1, 1])
        f8_xgrid = c7.checkbox("X-axis inner grey lines", True, key="pub_f8_xg")
        f8_ygrid = c8.checkbox("Y-axis inner grey lines", True, key="pub_f8_yg")
        f8_lbl_pos = c_lbl.selectbox("Label Position", ["inside", "outside", "none"], index=1, key="pub_f8_lbl")
        f8_legend = st.checkbox("Show legend", True, key="pub_f8_leg")
        f8_broken = st.checkbox("Enable Broken Y-Axis", False, key="pub_f8_broken")
        if f8_broken:
            c9, c10, c11 = st.columns(3)
            f8_bot_max = c9.number_input("Bottom Axis Max", value=float(y_min8 * 2.5 if y_min8 > 0 else 50), key="pub_f8_bmax")
            f8_top_min = c10.number_input("Top Axis Min", value=float(y_max8 * 0.8), key="pub_f8_tmin")
            f8_top_max = c11.number_input("Top Axis Max", value=float(y_max8 * 1.1), key="pub_f8_tmax")

        st.markdown("**Bar Colors**")
        color_cols = st.columns(3)
        f8_colors = []
        default_colors = ["#AED6F1", "#ABEBC6", "#F5B7B1"] # Blue, Green, Red
        for i, cat in enumerate(fig8_data["Category"]):
            f8_colors.append(color_cols[i % 3].color_picker(str(cat)[:15], default_colors[i % len(default_colors)], key=f"pub_f8_c_{i}"))

    fig8 = go.Figure()
    fig8.add_trace(go.Bar(
        x=fig8_data["Category"], y=fig8_data["Count"],
        marker_color=f8_colors,
        text=[str(int(x)) for x in fig8_data["Count"]] if f8_lbl_pos != "none" else None,
        textposition=f8_lbl_pos if f8_lbl_pos != "none" else None
    ))
    
    if f8_broken:
        fig8 = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08, row_heights=[0.7, 0.3])
        for i in [1, 2]:
            fig8.add_trace(go.Bar(
                x=fig8_data["Category"], y=fig8_data["Count"],
                marker_color=f8_colors,
                text=[str(int(x)) for x in fig8_data["Count"]] if f8_lbl_pos != "none" else None,
                textposition=f8_lbl_pos if f8_lbl_pos != "none" else None,
                showlegend=False
            ), row=i, col=1)
        fig8.update_yaxes(range=[f8_top_min, f8_top_max], row=1, col=1, title=f8_ytitle, showgrid=f8_ygrid, gridcolor='lightgray')
        fig8.update_yaxes(range=[0, f8_bot_max], row=2, col=1, title=f8_ytitle, showgrid=f8_ygrid, gridcolor='lightgray')
        fig8.update_xaxes(title=f8_xtitle, showgrid=f8_xgrid, gridcolor='lightgray', row=2, col=1)
        fig8.add_annotation(x=0, y=1.15, xref="paper", yref="paper", text="///", showarrow=False, font=dict(size=24, color="black"), ax=0, ay=0)
        fig8.update_layout(title=f8_title, template=PUB_THEME, font=dict(family="Arial", size=f8_fs, color="black"), width=f8_w, height=f8_h, bargap=0.4, showlegend=f8_legend)
    else:
        fig8.update_layout(title=f8_title, template=PUB_THEME, font=dict(family="Arial", size=f8_fs, color="black"),
            xaxis=dict(title=f8_xtitle, showgrid=f8_xgrid, gridcolor='lightgray'),
            yaxis=dict(title=f8_ytitle, showgrid=f8_ygrid, gridcolor='lightgray'),
            showlegend=f8_legend, width=f8_w, height=f8_h, bargroupgap=max(0, f8_bw-0.5))
    st.plotly_chart(fig8, use_container_width=False, config=PUB_CONFIG)

    # ---------------------------------------------------------
    # Figure 9: Cytokine Inducers
    # ---------------------------------------------------------
    st.markdown("### Figure 9: Cytokine Inducers (IL-2 & IFN-γ)")
    fig9_data = data["fig9"].copy()
    y_values9 = fig9_data["Count"].values
    y_max9 = max(y_values9) if len(y_values9) > 0 else 100
    y_min9 = min(y_values9) if len(y_values9) > 0 else 0

    with st.expander("Customize Figure 9"):
        c1, c2 = st.columns(2)
        f9_title = c1.text_input("Title", "Target Cytokine Inducers", key="pub_f9_t")
        f9_xtitle = c2.text_input("X-axis Title", "Category", key="pub_f9_xt")
        c3, c4 = st.columns(2)
        f9_ytitle = c3.text_input("Y-axis Title", "Peptide Count", key="pub_f9_yt")
        f9_fs = c4.slider("Font Size", 8, 24, 14, key="pub_f9_fs")
        f9_bw = st.slider("Bar Width", 0.1, 1.0, 0.6, key="pub_f9_bw")
        c5, c6 = st.columns(2)
        f9_w = c5.number_input("Width (px)", 200, 1600, 800, key="pub_f9_w")
        f9_h = c6.number_input("Height (px)", 200, 1600, 600, key="pub_f9_h")
        c7, c8, c_lbl = st.columns([1, 1, 1])
        f9_xgrid = c7.checkbox("X-axis inner grey lines", True, key="pub_f9_xg")
        f9_ygrid = c8.checkbox("Y-axis inner grey lines", True, key="pub_f9_yg")
        f9_lbl_pos = c_lbl.selectbox("Label Position", ["inside", "outside", "none"], index=1, key="pub_f9_lbl")
        f9_legend = st.checkbox("Show legend", True, key="pub_f9_leg")
        f9_broken = st.checkbox("Enable Broken Y-Axis", False, key="pub_f9_broken")
        if f9_broken:
            c9, c10, c11 = st.columns(3)
            f9_bot_max = c9.number_input("Bottom Axis Max", value=float(y_min9 * 2.5 if y_min9 > 0 else 50), key="pub_f9_bmax")
            f9_top_min = c10.number_input("Top Axis Min", value=float(y_max9 * 0.8), key="pub_f9_tmin")
            f9_top_max = c11.number_input("Top Axis Max", value=float(y_max9 * 1.1), key="pub_f9_tmax")

        st.markdown("**Bar Colors**")
        color_cols = st.columns(3)
        f9_colors = []
        default_colors = ["#D7DBDD", "#A9CCE3", "#A3E4D7"] # Gray, Blueish, Greenish
        for i, cat in enumerate(fig9_data["Category"]):
            f9_colors.append(color_cols[i % 3].color_picker(str(cat)[:15], default_colors[i % len(default_colors)], key=f"pub_f9_c_{i}"))

    fig9 = go.Figure()
    fig9.add_trace(go.Bar(
        x=fig9_data["Category"], y=fig9_data["Count"],
        marker_color=f9_colors,
        text=[str(int(x)) for x in fig9_data["Count"]] if f9_lbl_pos != "none" else None,
        textposition=f9_lbl_pos if f9_lbl_pos != "none" else None
    ))
    
    if f9_broken:
        fig9 = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08, row_heights=[0.7, 0.3])
        for i in [1, 2]:
            fig9.add_trace(go.Bar(
                x=fig9_data["Category"], y=fig9_data["Count"],
                marker_color=f9_colors,
                text=[str(int(x)) for x in fig9_data["Count"]] if f9_lbl_pos != "none" else None,
                textposition=f9_lbl_pos if f9_lbl_pos != "none" else None,
                showlegend=False
            ), row=i, col=1)
        fig9.update_yaxes(range=[f9_top_min, f9_top_max], row=1, col=1, title=f9_ytitle, showgrid=f9_ygrid, gridcolor='lightgray')
        fig9.update_yaxes(range=[0, f9_bot_max], row=2, col=1, title=f9_ytitle, showgrid=f9_ygrid, gridcolor='lightgray')
        fig9.update_xaxes(title=f9_xtitle, showgrid=f9_xgrid, gridcolor='lightgray', row=2, col=1)
        fig9.add_annotation(x=0, y=1.15, xref="paper", yref="paper", text="///", showarrow=False, font=dict(size=24, color="black"), ax=0, ay=0)
        fig9.update_layout(title=f9_title, template=PUB_THEME, font=dict(family="Arial", size=f9_fs, color="black"), width=f9_w, height=f9_h, bargap=0.4, showlegend=f9_legend)
    else:
        fig9.update_layout(title=f9_title, template=PUB_THEME, font=dict(family="Arial", size=f9_fs, color="black"),
            xaxis=dict(title=f9_xtitle, showgrid=f9_xgrid, gridcolor='lightgray'),
            yaxis=dict(title=f9_ytitle, showgrid=f9_ygrid, gridcolor='lightgray'),
            showlegend=f9_legend, width=f9_w, height=f9_h, bargroupgap=max(0, f9_bw-0.5))
    st.plotly_chart(fig9, use_container_width=False, config=PUB_CONFIG)

    # ---------------------------------------------------------
    # Figure 10: Pipeline Attrition Summary (Horizontal)
    # ---------------------------------------------------------
    st.markdown("### Figure 10: Pipeline Attrition Summary")
    with st.expander("Customize Figure 10"):
        c1, c2 = st.columns(2)
        f10_title = c1.text_input("Title", "Pipeline Attrition Summary", key="pub_f10_t")
        f10_xtitle = c2.text_input("X-axis Title", "Count", key="pub_f10_xt")
        c3, c4 = st.columns(2)
        f10_ytitle = c3.text_input("Y-axis Title", "Stage", key="pub_f10_yt")
        f10_fs = c4.slider("Font Size", 8, 24, 14, key="pub_f10_fs")
        f10_bw = st.slider("Bar Width", 0.1, 1.0, 0.6, key="pub_f10_bw")
        c5, c6 = st.columns(2)
        f10_w = c5.number_input("Width (px)", 200, 1600, 800, key="pub_f10_w")
        f10_h = c6.number_input("Height (px)", 200, 1600, 600, key="pub_f10_h")
        c7, c8, c_lbl = st.columns([1, 1, 1])
        f10_xgrid = c7.checkbox("X-axis inner grey lines", True, key="pub_f10_xg")
        f10_ygrid = c8.checkbox("Y-axis inner grey lines", True, key="pub_f10_yg")
        f10_lbl_pos = c_lbl.selectbox("Label Position", ["inside", "outside", "none"], index=1, key="pub_f10_lbl")
        f10_legend = st.checkbox("Show legend", True, key="pub_f10_leg")

        st.markdown("**Bar Colors**")
        color_cols = st.columns(7)
        f10_colors = []
        default_colors = [color_to_hex(c) for c in px.colors.qualitative.Pastel1 + px.colors.qualitative.Set3]
        for i, cat in enumerate(data["fig10"]["Category"]):
            f10_colors.append(color_cols[i % 7].color_picker(str(cat)[:10], default_colors[i % len(default_colors)], key=f"pub_f10_c_{i}"))

    fig10 = go.Figure()
    fig10.add_trace(go.Bar(
        y=data["fig10"]["Category"], x=data["fig10"]["Count"],
        orientation='h',
        marker_color=f10_colors,
        width=f10_bw,
        text=[str(int(x)) for x in data["fig10"]["Count"]] if f10_lbl_pos != "none" else None,
        textposition=f10_lbl_pos if f10_lbl_pos != "none" else None
    ))
    fig10.update_layout(
        title=f10_title, template=PUB_THEME,
        font=dict(family="Arial", size=f10_fs, color="black"),
        xaxis=dict(title=f10_xtitle, showgrid=f10_xgrid, gridcolor='lightgray'),
        yaxis=dict(title=f10_ytitle, showgrid=f10_ygrid, gridcolor='lightgray', autorange="reversed"),
        showlegend=f10_legend, width=f10_w, height=f10_h
    )
    st.plotly_chart(fig10, use_container_width=False, config=PUB_CONFIG)
