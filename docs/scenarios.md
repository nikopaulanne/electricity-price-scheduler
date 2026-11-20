# Price limits, cheapest hours, and heating schedules

Select a scenario and an [existing Home Assistant price source](configuration.md#profile-and-price-source) in the profile setup. Give each profile a name that describes the load or purpose, such as **Evening charging**. You can use one source in several profiles. Find the resulting entities on each profile's device page.

| Scenario | First setup | Later options |
| --- | --- | --- |
| Cheap now | Enter the price limit in the displayed currency per kWh. For example, 5 cents is 0.05 EUR/kWh. | Use a time window or selected weekdays. |
| Appliance cycle | Enter how long the appliance needs and the earliest and latest allowed times. | Add a price ceiling or restrict weekdays. |
| Water heater | Enter the total running time and daily window. The default uses one continuous run. | Split running time into shorter runs and choose a minimum run length. |
| EV charging | Enter the total charging time and when it must finish. The default may split the time across cheaper blocks. | Set the shortest run, a price ceiling, and whether a full duration can override that ceiling. |
| Heating and heat pump | Enter a maximum cutoff time and minimum price difference. | Use weather-based factors or let existing number helpers supply dynamic limits. |

A window such as 22:00–07:00 starts at 22:00 and ends the next morning. The weekday applies to the start of the window. When price coverage is missing, check the schedule status. A daily window ending at 07:00 does not guarantee operation by 07:00 if there is insufficient time or price coverage. If you configure a ceiling, choose **Keep the price limit** to respect it or **Prefer enough running time** to allow a more expensive plan when the ceiling prevents a complete schedule.

## Connect the signal to an automation

**Cheap now**, **Water heater**, **Appliance cycle**, and **EV charging** expose a **Use electricity now** binary sensor. To send a notification, trigger an automation when the signal changes to **On**. If an appliance runs its own uninterrupted cycle, use the signal to start the cycle or send a notification. For heating, see [Cutoff phase and Cutoff active](entities.md#understand-a-decision).

### First example with a switch

1. Create a **Cheap now** profile and enter `0.05 EUR/kWh` for a five-cent limit. Open the profile and compare **Current price** to that limit. **Use electricity now** is on while the price qualifies.
2. If you want the switch to follow the profile, open the [switch blueprint](../blueprints/automation/electricity_price_scheduler/follow_price_profile.yaml) on GitHub at the tag of your installed release. Copy the address of that file page. In Home Assistant, open **Settings → Automations & scenes → Blueprints → Import blueprint**, paste the address, and select **Preview**, then **Import blueprint**. For a local development build, [copy the blueprint to the test instance](development/local-testing.md#try-the-switch-blueprint-in-the-development-instance).
3. Create an automation from the blueprint. Select **Use electricity now** from your profile and choose the switch. The automation turns the switch off by default if the signal is unavailable. Clear that option to keep the current switch state until the signal returns.
4. Watch the first transition. Check the profile's signal and the switch state separately. If the signal is already on when you create the automation, use **Run actions** to apply it now after checking the selected switch.

To disconnect the switch, open the automation under **Settings → Automations & scenes** and turn the automation off. Set the switch to the state you want. You can remove that automation later and keep the profile to monitor prices.

### Try the signal with a Toggle helper

1. In **Settings → Devices & services → Helpers → Create helper**, choose **Toggle** and name it **Scheduler test load**.
2. In **Settings → Automations & scenes → Create automation**, start an empty automation. Add an **Entity → State** trigger for your profile's **Use electricity now** sensor and set **To** to **On**.
3. Add a **Perform action** action. Select **Input boolean: Turn on**, choose **Scheduler test load** as the target, and save. Make a second automation with **To: Off** and **Input boolean: Turn off** for the same target.
4. Use **Run actions** on either automation to check that the action sets **Scheduler test load** to the expected state. Open the helper under **Settings → Devices & services → Helpers** to see its state.
5. To test the triggers, open **Settings → Devices & services → Integrations → Electricity Price Scheduler** and select **Configure** next to the profile. In **Price profile settings**, set **Cheap price limit** above **Current price** if the signal is off, or below it if the signal is on. Select **Submit** and **Finish**, then check the signal and test load. Restore the original limit and check that both change back.
6. To stop the test, disable both automations and turn the helper off. This example reacts to **On** and **Off**. The [switch blueprint](#first-example-with-a-switch) can also respond when the signal becomes unavailable.

### Run a water heater for the cheapest three hours overnight

1. Create another **Electricity Price Scheduler** helper, choose **Water heater**, and select your price source.
2. Set **Total running time** to **180 minutes**, **Earliest start** to **22:00**, and **Finish by** to **07:00**. Water heater defaults to one uninterrupted run. Weekdays and maximum price can be set later in the profile options.
3. Submit to review the preview and submit again to save. On the profile's device page, check **Schedule status**, **Next run starts**, and **Next run ends**. If the run is already active, open **Running schedule** to see **Selected periods** and check that **Use electricity now** is **On**. A complete plan shows one three-hour run within your window. If the status is **Waiting for prices**, check again when your source publishes the rest of the window.

On the profile page, open **Schedule status** for the reason and **Next run starts** and **Next run ends** for the next scheduled time. Use **Current price** to compare the price with your limit. If the profile is waiting for prices, check back when the source publishes the rest of the window. See [troubleshooting](troubleshooting.md) if the source is unavailable.
