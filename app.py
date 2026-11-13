from datetime import datetime, timezone
import pandas as pd
import streamlit as st
from discovery import clinicaltrials, openalex, oncology_media_seeds, dedupe
from cli import classify_record

st.set_page_config(page_title="OncoDaily Discovery", page_icon="🔎", layout="wide")
st.title("OncoDaily: Competitor and Customer Discovery Engine")

with st.sidebar:
    st.header("Search")
    specialty = st.text_input("Specialty", value="oncology")
    preferred = st.selectbox("Classification", ["Both", "Potential customer", "Potential competitor", "Review required"])
    coverage = st.selectbox("Competitor coverage", ["Direct + adjacent", "Direct only"])
    per_source = st.slider("Results per source", 10, 50, 25, 5)
    use_trials = st.checkbox("ClinicalTrials.gov", value=False)
    use_companies = st.checkbox("OpenAlex US companies", value=True)
    use_openalex = st.checkbox("OpenAlex US institutions", value=False)
    use_media = st.checkbox("US competitor directory", value=True)
    model_path = st.text_input("Model path (optional)", value="", placeholder="models/classifier.joblib")
    run = st.button("Discover", type="primary", use_container_width=True)

if run:
    if not any([use_trials, use_openalex, use_companies, use_media]):
        st.warning("Pick at least one source.")
    else:
        results, errors = [], []
        sources = [
            (use_trials, "ClinicalTrials", lambda: clinicaltrials(specialty, per_source)),
            (use_openalex, "OpenAlex Inst", lambda: openalex(specialty, per_source)),
            (use_companies, "OpenAlex Comp", lambda: openalex(specialty, per_source, "company")),
            (use_media, "Directory", lambda: oncology_media_seeds(per_source)),
        ]
        
        with st.spinner("Searching..."):
            for enabled, name, fn in sources:
                if enabled:
                    try:
                        results.extend(fn())
                    except Exception as exc:
                        errors.append(f"{name}: {str(exc)[:150]}")

        classifier = None
        if model_path.strip():
            try:
                classifier = load_model(model_path.strip())
            except Exception as exc:
                errors.append(f"Model failed: {exc}. Using rules instead.")

        scored = [classify_record(r, specialty, preferred, classifier) for r in dedupe(results)]
        scored = [r for r in scored if r]
        
        if coverage == "Direct only":
            scored = [r for r in scored if r["category"] != "Potential competitor" or r.get("competition_tier") == "Direct"]

        st.session_state["results"] = sorted(scored, key=lambda x: x["relevance_score"], reverse=True)
        st.session_state["errors"] = errors

for err in st.session_state.get("errors", []):
    st.warning(err)

rows = st.session_state.get("results")
if rows is None:
    st.info("Set search criteria and click **Discover**.")
elif not rows:
    st.warning("No matches found.")
else:
    df = pd.DataFrame(rows)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Candidates", len(df))
    c2.metric("Customers", int((df.category == "Potential customer").sum()))
    c3.metric("Direct Rivals", int(((df.category == "Potential competitor") & (df.competition_tier == "Direct")).sum()))
    c4.metric("Adjacent Leads", int(((df.category == "Potential competitor") & (df.competition_tier == "Adjacent")).sum()))

    cols = ["name", "category", "competition_tier", "parent_organization", "relevance_score", "kind", "country", "description", "rationale", "source", "source_url", "website"]
    st.dataframe(
        df[cols].rename(columns={"relevance_score": "score"}),
        hide_index=True,
        column_config={"source_url": st.column_config.LinkColumn("source_url"), "website": st.column_config.LinkColumn("website")},
        use_container_width=True
    )
    st.download_button(
        "Download CSV",
        data=df[cols].to_csv(index=False).encode("utf-8-sig"),
        file_name=f"oncodaily_export_{datetime.now(timezone.utc):%Y%m%d}.csv",
        mime="text/csv"
    )