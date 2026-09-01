import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams.update({"font.family": "serif", "font.size": 21,
                     "axes.spines.top": False, "axes.spines.right": False})

x = [0, 1]; labels = ["nano", "mini"]
f1     = {"single": [0.7104, 0.9341], "star": [0.7560, 0.5900]}
tokens = {"single": [41.1, 20.7],     "star": [65.6, 139.5]}
compl  = {"single": [97.5, 100.0],    "star": [100.0, 70.0]}

fig, axes = plt.subplots(1, 3, figsize=(19.2, 6.6))
style = {"single": dict(color="black",   marker="o", ls="-",  lw=3.2, ms=13),
         "star":   dict(color="#8a8a8a", marker="s", ls="--", lw=3.2, ms=13)}
disp  = {"single": "single-agent", "star": "star"}

for ax, data, title, fmt, ylim in [
    (axes[0], f1,     r"Macro selection $F_1$",          "{:.2f}", (0.55, 1.02)),
    (axes[1], tokens, "Mean tokens per run (thousands)", "{:.0f}", (0, 158)),
    (axes[2], compl,  "Completion (%)",                  "{:.0f}", (0, 112)),
]:
    for arm in ("single", "star"):
        other = "star" if arm == "single" else "single"
        ax.plot(x, data[arm], label=disp[arm], **style[arm])
        for i, (xi, yi) in enumerate(zip(x, data[arm])):
            higher = yi >= data[other][i]
            off = 15 if higher else -30
            ax.annotate(fmt.format(yi), (xi, yi), textcoords="offset points",
                        xytext=(0, off), ha="center",
                        color=style[arm]["color"], fontsize=19)
    ax.set_title(title, pad=16)
    ax.set_xticks(x); ax.set_xticklabels(labels)
    ax.set_xlim(-0.34, 1.34); ax.set_ylim(*ylim)

axes[1].legend(frameon=False, loc="upper left", fontsize=19)
fig.tight_layout()
fig.savefig("docs/dissertation/fig_capability.png", dpi=220, bbox_inches="tight")
print("ok")
