# README

# Competitor & Customer Discovery

A Python app that finds and ranks **US-based** oncology companies, publications, and other potential customers or competitors.

Results come from online searches.

Open Terminal in the corresponding folder and run:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

Open [**http://localhost:8501](http://localhost:8501/).** 

Results are saved locally in SQLite and can be exported as CSV.