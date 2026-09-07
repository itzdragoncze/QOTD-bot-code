# Privacy Policy

*Last Updated: September 7, 2026*

This Privacy Policy explains how **DailyDrop - QOTD** ("the Bot", "we", "us", or "our") collects, uses, stores, and protects data when you use the Bot across Discord servers, interactive commands, and community features.

We value your privacy and are committed to maintaining transparent, minimal, and secure data collection practices strictly limited to what is necessary for the Bot's core functionality.

---

## 1. Information We Collect

The Bot only collects data necessary to deliver daily question scheduling, community question submissions, administrative review, and localized interface rendering.

### A. Server (Guild) Information
- **Guild ID**: Required to associate settings, configured QOTD channels, questions, and suggestions with the server.
- **Channel IDs**: IDs of text channels registered for QOTD posts, discussion thread creation, and administrative review cards.
- **Role IDs**: Configured subscriber notification roles and suggestion-restricted roles.
- **Server Configuration**: Default language preference, scheduled posting times, low-queue alert thresholds, and channel queue limits.

### B. User & Interaction Information
When you propose a question via `/qotd_suggest` or the interactive Suggest button:
- **Discord User ID**: To identify the suggester, enforce per-user pending submission limits (max 3 pending), and route approval/rejection DMs.
- **Display Name / Username**: Stored to provide author attribution on questions and review cards.
- **Avatar URL**: Stored temporarily to render the suggester's avatar in administrative review embeds.
- **Language Preference**: Detected client locale to deliver DM status updates in your preferred language.

When an administrator adds or edits a question manually:
- **Admin User ID & Display Name**: Recorded for internal audit logging (`added_by_id`, `added_by_name`).

### C. Content Data
- **Question Text**: Submitted question content (up to 300 characters).
- **Suggestion Notes**: Optional user notes or source URLs attached to suggestions.
- **Timestamps**: Creation dates, scheduled post timestamps, and publication dates.

---

## 2. Information We DO NOT Collect

To ensure privacy and compliance with Discord's developer requirements:
- **No Private Message Scraping**: The Bot does not read, log, or monitor general server messages, member chats, or direct messages outside of explicit command interactions with the Bot.
- **No Privileged Message Content**: The Bot operates with the Discord `Message Content` intent disabled.
- **No Personal Identifiers**: We do not collect emails, real names, phone numbers, IP addresses, or payment information.
- **No Voice or Media Data**: The Bot does not join voice channels or record audio.

---

## 3. How We Use Collected Information

Collected data is used solely to:
1. Schedule and automatically publish Question of the Day posts in designated channels.
2. Maintain question queues, history archives, and prevent duplicate submissions.
3. Manage the administrative review workflow for community suggestions.
4. Send automated direct message (DM) notifications to users when their suggestions are approved, edited, or declined.
5. Provide server administrators with dashboard controls, search capabilities, and database capacity statistics.
6. Enforce anti-abuse limits (e.g., duplicate detection, maximum queue capacity, rate limits).

---

## 4. Data Sharing & Disclosure

- **No Sale of Data**: We do **not** sell, rent, monetize, or trade any user or server data to third parties, advertisers, or data brokers.
- **Discord API**: Data is transmitted through Discord's official APIs to deliver messages and process interactions in accordance with Discord's [Developer Terms](https://discord.com/developers/docs/policies-and-agreements/developer-terms-of-service).
- **Legal Compliance**: We will only disclose stored data if required by applicable law, court order, or official governmental request.

---

## 5. Data Storage, Security & Retention

### A. Storage & Security
- Data is stored in a structured SQLite database hosted in an isolated, secure environment.
- Access to the database is strictly restricted to authorized bot maintainers.
- All communications between Discord and the Bot are encrypted in transit via Discord's secure HTTPS/WSS protocols.

### B. Retention Periods
- **Active Questions**: Maintained in the queue or history until deleted by a server administrator or upon channel unlinking.
- **Pending Suggestions**: Stored in the review queue until approved or rejected by an administrator.
- **Declined Suggestions**: Permanently purged from the database upon administrator rejection.
- **Approved Suggestions**: Converted into queue entries; temporary review metadata is removed.

---

## 6. User Rights & Data Deletion (GDPR & CCPA Alignment)

We respect your privacy rights and provide easy mechanisms to manage or remove your data:

1. **Right to Erasure (Deletion)**:
   - Server administrators can clear channels, questions, or history at any time using the `/qotd` control panel (`Bulk Delete` or `Clear Category`).
   - Server administrators can unlink channels via Channel Settings, with an option to purge all associated questions and suggestions.
   - Individual users can request complete removal of their User ID and attribution from stored questions by contacting the server administrator or bot maintainer.
2. **Right of Removal**: Removing (kicking/banning) the Bot from a server halts all data processing for that server. Server data can be permanently erased upon request.
3. **Right to Access**: Server administrators can inspect all stored questions, suggestions, and history directly through the interactive `/qotd` dashboard.

---

## 7. Children's Privacy

The Bot is not intended for use by individuals under the minimum age required by Discord's Terms of Service (13 years of age or older, depending on local jurisdiction). We do not knowingly collect personal data from children under these age thresholds.

---

## 8. Changes to This Privacy Policy

We may update this Privacy Policy periodically to reflect new features, operational practices, or legal requirements. Updates will be reflected in this file with an updated "Last Updated" date. Continued interaction with the Bot after modifications signifies acceptance of the updated policy.

---

## 9. Contact Us

If you have any questions, concerns, or requests regarding this Privacy Policy or your data, please contact us:
- **Email**: [itzdragoncze@gmail.com](mailto:itzdragoncze@gmail.com)
- **Discord Support Server**: [https://discord.gg/z5M7umJMza](https://discord.gg/z5M7umJMza)
