"""Single-agent ReAct baseline.

One LLM drives a think → act → observe loop over the four registered
tools until it calls ``finalise`` or hits a budget / iteration limit.
"""
