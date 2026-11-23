# Project Roadmap

> **Vision:** Create a system to detect, stream, record and present live heartrate data for livestreaming purposes  
> **Status Legend:** 🟢 Completed | 🟡 Planned | ⚪ Backlog 
---
# Roadmap Overview

```mermaid
graph LR
    Root["🎯 Project Vision"]

    Root --> Completed["🟢 Completed"]
    Root --> Next["🟡 Next Steps"]
    Root --> Future["⚪ Future Plans"]

    %% Completed Plans
    Completed --> comp1["Basic client code"]
    Completed --> comp2["Basic server code"]
    comp1 & comp2 --> M1(["Init Release (v0.0.2)"]):::doneMilestone

    Completed --> comp3["Operation tweaks"]
    M1 & comp3 --> M2(["First Version (v1.0.0)"]):::doneMilestone

    Completed --> comp4["User settings"]
    Completed --> comp5["Downloadable Executable"]
    M2 & comp4 & comp5 --> M3(["(v1.0.0)"]):::doneMilestone

    Completed --> comp6["New Features"]
    M3 & comp6 --> M4(["Auto Connect (v1.1.0)"]):::doneMilestone

    Completed --> comp7["Updated Namings"]
    M4 & comp7 --> M5(["Generic Namin (v1.1.1)"]):::doneMilestone

    Completed --> comp8["Redone UI"]
    Completed --> comp9["Redon server UI"]
    Completed --> comp10["OBS Overlay"]
    M5 & comp8 & comp9 & comp10 --> M6(["Updated layout and colors (v1.2.0)"]):::doneMilestone



    %% Next branch
    Next --> next1["User Testing"]
    Next --> next2["Documentation"]

    %% Future Plans branch
    Future --> future1["Mobile Support"]
    Future --> future2["Analytics Dashboard"]

    %% Milestone Styles
    classDef doneMilestone fill:#e6f4ea,stroke:#1e8e3e,stroke-width:2px,color:#137333,font-weight:bold
    classDef nextMilestone fill:#fef7e0,stroke:#f9ab00,stroke-width:3px,color:#b06000,font-weight:bold
    classDef futureMilestone fill:#e8f0fe,stroke:#1a73e8,stroke-width:3px,color:#174ea6,font-weight:bold
```


---
## 🎯 Core Goals
- **Goal 1:** [Primary outcome or metric — e.g., Ship a usable core workflow]
- **Goal 2:** [Secondary outcome or metric — e.g., Validate with early users]
- **Goal 3:** [Quality or reliability target — e.g., Full documentation and stable release]

---
## Future of the Development

### 🟡 Next Steps
- [ ] Test with mnore devices
- [ ] Update the readme with install/use manual
- [ ] hjdsjkfj


### ⚪ Future Plans
- [ ] Make "Help" buttons in the client program for help features/tutorials

---

## ⚠️ Risks & Mitigations

| Risk | Impact | Mitigation |
| :--- | :--- | :--- |
| Scope creep during core build | High | Freeze MVP scope until Milestone 1 ships |
| External dependencies | Medium | Build mock endpoints so work can proceed in parallel |
