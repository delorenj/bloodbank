// n8n Code Node
// This script transforms a Fireflies transcription JSON into both CSV and Markdown formats.

// The input data from the previous node is available in the 'items' array.
const inputData = items[0].json;

// The relevant transcription data is nested within the JSON structure.
const data = inputData.content.data;
const sentences = data.sentences;
const meetingDate = new Date(data.dateString || data.date);

// --- Helper Functions ---

/**
 * Formats a duration in seconds into a "mm:ss" string.
 * @param {number} seconds - The duration in seconds.
 * @returns {string} The formatted time string.
 */
function formatTime(seconds) {
  const minutes = Math.floor(seconds / 60).toString().padStart(2, '0');
  const secs = Math.round(seconds % 60).toString().padStart(2, '0');
  return `${minutes}:${secs}`;
}

/**
 * Returns the ordinal suffix for a given day of the month (e.g., "st", "nd", "rd", "th").
 * @param {number} i - The day of the month.
 * @returns {string} The ordinal suffix.
 */
function getOrdinalSuffix(i) {
    const j = i % 10,
        k = i % 100;
    if (j == 1 && k != 11) {
        return "st";
    }
    if (j == 2 && k != 12) {
        return "nd";
    }
    if (j == 3 && k != 13) {
        return "rd";
    }
    return "th";
}

// --- CSV Generation ---

const csvHeaders = ['"sentence"', '"startTime"', '"endTime"', '"speaker_id"', '"speaker_name"'];
let csvContent = csvHeaders.join(',') + '\n';

for (const sentence of sentences) {
  // Use new RegExp to avoid issues with forward slashes in strings.
  const sentenceText = `"${sentence.raw_text.replace(new RegExp('"', 'g'), '""')}"`;
  const startTime = `"${formatTime(sentence.start_time)}"`;
  const endTime = `"${formatTime(sentence.end_time)}"`;
  const speakerId = sentence.speaker_id;
  const speakerName = `"${sentence.speaker_name || 'speaker 1'}"`;
  const row = [sentenceText, startTime, endTime, speakerId, speakerName];
  csvContent += row.join(',') + '\n';
}

csvContent += ',,,,\n';

const day = meetingDate.getDate();
const month = meetingDate.toLocaleString('default', { month: 'short' });
const year = meetingDate.getFullYear();
const time = meetingDate.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: true });
const formattedCsvDate = `"${day}${getOrdinalSuffix(day)} ${month}, ${year} - ${time}"`;

csvgContent += `"meeting created at",${formattedCsvDate},,,\n`;

// --- Markdown Generation ---

let markdownContent = `# Transcript: ${data.title}\n\n`;

const markdownDate = meetingDate.toLocaleDateString('en-US', {
  year: 'numeric',
  month: 'long',
  day: 'numeric'
});

markdownContent += `**Date:** ${markdownDate}\n`;
// The duration in the Fireflies payload appears to be in minutes.
markdownContent += `**Duration:** ${Math.round(data.duration * 60)} seconds\n`;
markdownContent += `**Transcript URL:** [View Transcript](${data.transcript_url})\n\n`;
markdownContent += '---\n\n';

for (const sentence of sentences) {
  const speaker = sentence.speaker_name || 'Unknown Speaker';
  const timestamp = formatTime(sentence.start_time);
  markdownContent += `**${speaker}** (${timestamp}): ${sentence.text}\n\n`;
}

// --- Return both outputs ---
// The next n8n node will have access to `csv` and `markdown` properties in the JSON.
return [{
  json: {
    csv: csvContent,
    markdown: markdownContent
  }
}];
