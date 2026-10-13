"""The I13 summary report: one readable page over the four I13 screens.

Three steps, and only the middle one touches a model:

1. ``facts``     -- every figure, read from the same snapshot the screens serve.
2. ``narrative`` -- prose over those figures. A model may write it; it may not
                    introduce a number the facts do not hold, and a template
                    stands in whenever no model answers or one breaks that rule.
3. ``store``     -- the result is saved, so a report can be reopened, shared
                    and traced to the prompt and model behind its words.

``export`` turns a saved report into a workbook for people who want the rows.
"""
